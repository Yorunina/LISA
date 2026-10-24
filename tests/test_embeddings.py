import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa import model_calls
from lisa.model_calls import (
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    ModelInputTooLarge,
    generate_embeddings,
    generate_reply,
)
from lisa.models import Account, BudgetDay, ModelCall, Task
from lisa.retrieval import embedding_space, indexing_enabled


@pytest.fixture
def embedding_settings(settings):
    return settings.model_copy(
        update={
            "model_base_url": "https://embedding.invalid/v1",
            "model_api_key": SecretStr("synthetic-key"),
            "model_name": "synthetic-response-model",
            "embedding_model": "synthetic-embedding-model",
            "embedding_dimensions": 3,
            "embedding_input_per_million": Decimal("10"),
            "model_daily_usd": Decimal("0.5"),
            "model_task_usd": Decimal("0.15"),
            "model_concurrency": 1,
        }
    )


@pytest.fixture
def task_account(db):
    user = Account(username="embedding_test", password_hash="synthetic-unused")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, kind="index", status="running")
    db.add(task)
    db.commit()
    return task.id, user.id


def response(*, data=None, tokens=20):
    return SimpleNamespace(
        data=data if data is not None else [SimpleNamespace(index=0, embedding=[1.0, 0.5, -0.5])],
        usage=SimpleNamespace(prompt_tokens=tokens),
    )


def records(engine):
    with Session(engine) as db:
        return list(db.scalars(select(ModelCall).order_by(ModelCall.started_at))), list(
            db.scalars(select(BudgetDay))
        )


@pytest.fixture
def sdk(monkeypatch):
    configurations = []
    embeddings = AsyncMock(return_value=response())
    responses = AsyncMock(
        return_value=SimpleNamespace(
            status="completed",
            output_text=json.dumps({"reply": "好。"}),
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )
    )

    class FakeClient:
        def __init__(self, **kwargs):
            configurations.append(kwargs)
            self.embeddings = SimpleNamespace(create=embeddings)
            self.responses = SimpleNamespace(create=responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(model_calls, "AsyncOpenAI", FakeClient)
    return SimpleNamespace(
        embeddings=embeddings, responses=responses, configurations=configurations
    )


@pytest.mark.asyncio
async def test_ordered_vectors_and_shared_cost_metadata(
    embedding_settings, engine, task_account, sdk
):
    async def provider(**kwargs):
        calls, days = records(engine)
        assert calls[0].state == "reserved" and days[0].reserved_usd > 0
        assert kwargs == {
            "model": "synthetic-embedding-model",
            "input": ["first", "second"],
            "dimensions": 3,
            "encoding_format": "float",
        }
        return response(
            data=[
                SimpleNamespace(index=1, embedding=[0, 1, 0]),
                SimpleNamespace(index=0, embedding=[1, 0, 0]),
            ],
            tokens=100,
        )

    sdk.embeddings.side_effect = provider
    result = await generate_embeddings(
        embedding_settings, engine, *task_account, ["first", "second"]
    )
    assert result == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    calls, days = records(engine)
    assert calls[0].state == "succeeded" and calls[0].input_tokens == 100
    assert calls[0].output_tokens == 0 and calls[0].charged_usd == Decimal("0.001000")
    assert days[0].reserved_usd == 0 and days[0].spent_usd == calls[0].charged_usd
    assert calls[0].configuration == {
        "api": "embeddings",
        "model": "synthetic-embedding-model",
        "base_url": "https://embedding.invalid/v1",
        "dimensions": 3,
        "input_per_million": "10",
        "output_per_million": "0",
    }
    assert sdk.configurations[0]["max_retries"] == 0
    assert sdk.configurations[0]["http_client"].follow_redirects is False


@pytest.mark.asyncio
async def test_separate_embedding_provider_preserves_chat_credentials(
    embedding_settings, engine, task_account, sdk
):
    separate = embedding_settings.model_copy(
        update={
            "embedding_base_url": "https://separate.invalid/v1",
            "embedding_api_key": SecretStr("separate-synthetic-key"),
            "embedding_batch_size": 10,
        }
    )
    assert indexing_enabled(separate)
    assert embedding_space(separate) != embedding_space(embedding_settings)
    assert embedding_space(separate) == embedding_space(
        separate.model_copy(update={"model_base_url": "https://changed-chat.invalid/v1"})
    )
    await generate_embeddings(separate, engine, *task_account, ["text"])
    await generate_reply(separate, engine, *task_account, "Reply briefly", [])
    assert [(c["base_url"], c["api_key"]) for c in sdk.configurations] == [
        ("https://separate.invalid/v1", "separate-synthetic-key"),
        ("https://embedding.invalid/v1", "synthetic-key"),
    ]
    calls, _ = records(engine)
    assert calls[0].configuration["base_url"] == "https://separate.invalid/v1"
    with pytest.raises(ModelInputTooLarge):
        await generate_embeddings(separate, engine, *task_account, ["text"] * 11)
    assert len(records(engine)[0]) == 2


@pytest.mark.asyncio
async def test_embeddings_and_responses_share_task_call_limit(
    embedding_settings, engine, task_account, sdk
):
    await generate_reply(
        embedding_settings, engine, *task_account, "简短回复", [{"role": "user", "content": "好"}]
    )
    await generate_embeddings(embedding_settings, engine, *task_account, ["one"])
    await generate_embeddings(embedding_settings, engine, *task_account, ["two"])
    with pytest.raises(BudgetExceeded):
        await generate_embeddings(embedding_settings, engine, *task_account, ["three"])
    calls, _ = records(engine)
    assert len(calls) == 3 and sdk.embeddings.await_count == 2 and sdk.responses.await_count == 1


@pytest.mark.parametrize("scope", ["daily", "task"])
@pytest.mark.asyncio
async def test_embedding_reservation_honors_shared_allowance(
    embedding_settings, engine, db, task_account, sdk, scope
):
    day = BudgetDay(day=datetime.now(UTC).date(), spent_usd=Decimal("0.499"))
    db.add(day)
    if scope == "task":
        day.spent_usd = Decimal("0.149")
        db.flush()
        db.add(
            ModelCall(
                task_id=task_account[0],
                user_id=task_account[1],
                budget_day=day.day,
                state="succeeded",
                reserved_usd=Decimal("0.149"),
                charged_usd=Decimal("0.149"),
            )
        )
    db.commit()
    with pytest.raises(BudgetExceeded):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    sdk.embeddings.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_uses_global_inflight_slot(embedding_settings, engine, task_account, sdk):
    reserved = model_calls._reserve(embedding_settings, engine, *task_account, Decimal("0.01"))
    with pytest.raises(ModelBusy):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, _ = records(engine)
    assert [call.id for call in calls] == [reserved]
    sdk.embeddings.assert_not_awaited()


@pytest.mark.parametrize("code,unknown", [(400, False), (429, False), (408, True), (500, True)])
@pytest.mark.asyncio
async def test_embedding_http_failure_accounting(
    embedding_settings, engine, task_account, sdk, code, unknown
):
    sdk.embeddings.side_effect = APIStatusError(
        "synthetic",
        response=httpx2.Response(code, request=httpx2.Request("POST", "https://embedding.invalid")),
        body=None,
    )
    with pytest.raises(ModelCallUnknown if unknown else ModelCallFailed):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, days = records(engine)
    assert calls[0].state == ("unknown" if unknown else "failed")
    assert calls[0].charged_usd == (calls[0].reserved_usd if unknown else 0)
    assert days[0].reserved_usd == 0
    if unknown:
        with pytest.raises(ModelCallUnknown):
            await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
        assert sdk.embeddings.await_count == 1


@pytest.mark.parametrize(
    "cause,unknown", [(httpx2.ConnectError, False), (httpx2.ReadTimeout, True)]
)
@pytest.mark.asyncio
async def test_embedding_transport_failure_accounting(
    embedding_settings, engine, task_account, sdk, cause, unknown
):
    async def fail(**kwargs):
        raise APIConnectionError(
            request=httpx2.Request("POST", "https://embedding.invalid")
        ) from cause("synthetic")

    sdk.embeddings.side_effect = fail
    with pytest.raises(ModelCallUnknown if unknown else ModelCallFailed):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, _ = records(engine)
    assert calls[0].charged_usd == (calls[0].reserved_usd if unknown else 0)


@pytest.mark.parametrize("not_sent", [False, True])
@pytest.mark.asyncio
async def test_embedding_cancellation_reserves_unknown_cost(
    embedding_settings, engine, task_account, sdk, not_sent
):
    async def cancel(**kwargs):
        if not_sent:
            raise asyncio.CancelledError() from httpx2.ConnectError("synthetic")
        raise asyncio.CancelledError()

    sdk.embeddings.side_effect = cancel
    with pytest.raises(asyncio.CancelledError):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, _ = records(engine)
    assert calls[0].state == ("failed" if not_sent else "unknown")
    assert calls[0].charged_usd == (0 if not_sent else calls[0].reserved_usd)


@pytest.mark.parametrize(
    "data",
    [
        [],
        [SimpleNamespace(index=1, embedding=[1, 0, 0])],
        [SimpleNamespace(index=0, embedding=[1, 0])],
        [SimpleNamespace(index=0, embedding=[0, 0, 0])],
        [SimpleNamespace(index=0, embedding=[True, 0, 0])],
        [SimpleNamespace(index=0, embedding=[float("nan"), 0, 0])],
        [SimpleNamespace(index=0, embedding=[float("inf"), 0, 0])],
        [SimpleNamespace(index=0, embedding=["1", 0, 0])],
    ],
)
@pytest.mark.asyncio
async def test_invalid_paid_vectors_still_charged(
    embedding_settings, engine, task_account, sdk, data
):
    sdk.embeddings.return_value = response(data=data)
    with pytest.raises(ModelCallFailed):
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, days = records(engine)
    assert calls[0].state == "failed" and calls[0].error == "invalid_embeddings"
    assert calls[0].charged_usd == Decimal("0.000200") and days[0].spent_usd == Decimal("0.000200")


@pytest.mark.asyncio
async def test_duplicate_response_index_rejected(embedding_settings, engine, task_account, sdk):
    sdk.embeddings.return_value = response(data=[SimpleNamespace(index=0, embedding=[1, 0, 0])] * 2)
    with pytest.raises(ModelCallFailed):
        await generate_embeddings(embedding_settings, engine, *task_account, ["one", "two"])


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.asyncio
async def test_missing_usage_keeps_reservation_charge(
    embedding_settings, engine, task_account, sdk, valid
):
    sdk.embeddings.return_value = response(tokens=None, data=None if valid else [])
    if valid:
        await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    else:
        with pytest.raises(ModelCallFailed):
            await generate_embeddings(embedding_settings, engine, *task_account, ["text"])
    calls, _ = records(engine)
    assert calls[0].charged_usd == calls[0].reserved_usd and calls[0].input_tokens is None


@pytest.mark.parametrize("texts", [[], ["x"] * 33, ["雨" * 11000]])
@pytest.mark.asyncio
async def test_embedding_input_bounded_before_reservation(
    embedding_settings, engine, task_account, sdk, texts
):
    with pytest.raises(ModelInputTooLarge):
        await generate_embeddings(embedding_settings, engine, *task_account, texts)
    assert records(engine)[0] == []
    sdk.embeddings.assert_not_awaited()


@pytest.mark.parametrize(
    "update",
    [
        {"embedding_model": ""},
        {"embedding_input_per_million": Decimal(0)},
        {"embedding_base_url": "https://no-key.invalid/v1"},
        {"embedding_api_key": SecretStr("no-endpoint-synthetic-key")},
    ],
)
@pytest.mark.asyncio
async def test_embedding_placeholder_config_does_not_call_provider(
    embedding_settings, engine, task_account, sdk, update
):
    assert not indexing_enabled(embedding_settings.model_copy(update=update))
    with pytest.raises(ModelCallFailed):
        await generate_embeddings(
            embedding_settings.model_copy(update=update), engine, *task_account, ["text"]
        )
    assert records(engine)[0] == []
    sdk.embeddings.assert_not_awaited()
