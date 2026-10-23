import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError
from openai.types.responses.response import Response
from openai.types.responses.response_usage import ResponseUsage
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa import model_calls
from lisa.model_calls import (
    MAX_INPUT_BYTES,
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    generate_reply,
    trim_history,
)
from lisa.models import Account, BudgetDay, ModelCall, Task


@pytest.fixture
def model_settings(settings):
    return settings.model_copy(
        update={
            "model_base_url": "https://model.invalid/v1",
            "model_api_key": SecretStr("synthetic-key"),
            "model_name": "gpt-6-astra",
            "model_daily_usd": Decimal("0.50"),
            "model_task_usd": Decimal("0.15"),
            "model_input_per_million": Decimal("10"),
            "model_output_per_million": Decimal("50"),
            "model_max_output_tokens": 600,
            "model_concurrency": 1,
            "model_timeout_seconds": 60,
        }
    )


@pytest.fixture
def task_account(db):
    user = Account(username="model_test", password_hash="synthetic-test-hash")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, kind="chat", source="qq", status="running")
    db.add(task)
    db.commit()
    return task.id, user.id


def test_distillation_uses_shared_daily_budget_without_individual_money_cap(
    model_settings, engine, db, task_account
):
    chat_id, user_id = task_account
    style = Task(user_id=user_id, kind="style_distill", source="web", status="running")
    db.add(style)
    db.commit()
    with pytest.raises(BudgetExceeded):
        model_calls._reserve(model_settings, engine, chat_id, user_id, Decimal("0.20"))
    model_calls._reserve(model_settings, engine, style.id, user_id, Decimal("0.20"))
    with pytest.raises(BudgetExceeded, match="Daily"):
        model_calls._reserve(model_settings, engine, style.id, user_id, Decimal("0.35"))
    db.expire_all()
    day = db.scalar(select(BudgetDay))
    assert day.reserved_usd == Decimal("0.20")
    assert len(db.scalars(select(ModelCall)).all()) == 1


def response(*, reply="可以，我们慢慢写。", status="completed", usage=True):
    return SimpleNamespace(
        status=status,
        output_text=json.dumps({"reply": reply}),
        usage=SimpleNamespace(input_tokens=100, output_tokens=20) if usage else None,
    )


@pytest.fixture
def sdk(monkeypatch):
    calls = []
    create = AsyncMock(return_value=response())

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.responses = SimpleNamespace(create=create)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(model_calls, "AsyncOpenAI", FakeClient)
    return SimpleNamespace(create=create, configurations=calls)


async def generate(settings, engine, task_account):
    return await generate_reply(
        settings,
        engine,
        *task_account,
        instructions="用简短中文交流。",
        messages=[{"role": "user", "content": "今天想写一个开头。"}],
    )


@pytest.mark.asyncio
async def test_author_schema_and_output_budget_share_the_same_ledger(
    model_settings,
    engine,
    task_account,
    sdk,
):
    from lisa.author import WritingResult, generate_writing

    result = WritingResult(
        title="短场景", paragraphs=["他捡起信。"], summary="他收到信。", plot_state="尚未拆开。"
    )
    sdk.create.return_value = SimpleNamespace(
        status="completed",
        output_text=result.model_dump_json(),
        usage=SimpleNamespace(input_tokens=120, output_tokens=80),
    )
    actual = await generate_writing(
        model_settings,
        engine,
        *task_account,
        "写一个场景",
        [{"role": "user", "content": "雨中收信"}],
    )
    assert actual == result
    request = sdk.create.call_args.kwargs
    assert request["max_output_tokens"] == model_settings.writing_output_tokens
    assert "paragraphs" in request["text"]["format"]["schema"]["properties"]
    assert "title" in request["text"]["format"]["schema"]["properties"]
    calls, days = records(engine)
    assert calls[0].configuration["max_output_tokens"] == model_settings.writing_output_tokens
    assert calls[0].reserved_usd >= Decimal("0.060000")
    assert days[0].reserved_usd == 0 and days[0].spent_usd == Decimal("0.005200")


def records(engine):
    with Session(engine) as db:
        return db.scalars(select(ModelCall).order_by(ModelCall.started_at)).all(), db.scalars(
            select(BudgetDay).order_by(BudgetDay.day)
        ).all()


@pytest.mark.asyncio
async def test_reservation_precedes_request_and_usage_settles_cost(
    model_settings, engine, task_account, sdk
):
    async def provider(**kwargs):
        calls, days = records(engine)
        assert calls[0].state == "reserved"
        assert days[0].reserved_usd == calls[0].reserved_usd > Decimal("0.03")
        assert kwargs["store"] is False
        assert kwargs["max_output_tokens"] == 600
        assert kwargs["reasoning"] == {"effort": "low"}
        assert kwargs["text"]["format"]["strict"] is True
        return response()

    sdk.create.side_effect = provider
    assert await generate(model_settings, engine, task_account) == "可以，我们慢慢写。"
    calls, days = records(engine)
    assert calls[0].state == "succeeded"
    assert calls[0].input_tokens == 100 and calls[0].output_tokens == 20
    assert calls[0].charged_usd == Decimal("0.002000")
    assert calls[0].configuration == {
        "model": "gpt-6-astra",
        "base_url": "https://model.invalid/v1",
        "max_output_tokens": 600,
        "input_per_million": "10",
        "output_per_million": "50",
    }
    assert days[0].spent_usd == calls[0].charged_usd
    assert days[0].reserved_usd == 0
    assert sdk.configurations[0]["max_retries"] == 0
    assert sdk.configurations[0]["timeout"] == 60
    assert sdk.configurations[0]["http_client"].follow_redirects is False


@pytest.mark.asyncio
async def test_history_trimming_uses_actual_request_size_and_keeps_current_input(
    model_settings, engine, task_account, sdk
):
    instructions = "完整性格与系统指令"
    messages = [
        {"role": "user", "content": "旧" * 4000},
        {"role": "assistant", "content": "最近的完整回复"},
        {"role": "user", "content": "新" * 3000},
    ]
    selected = trim_history(model_settings, instructions, messages, history_count=2)
    assert selected == messages[1:]
    assert len(messages) == 3
    await generate_reply(model_settings, engine, *task_account, instructions, selected)
    request = sdk.create.call_args.kwargs
    assert request["instructions"] == instructions
    assert request["input"][-1]["content"] == "新" * 3000
    assert (
        len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
        <= MAX_INPUT_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["model_daily_usd", "model_task_usd"])
async def test_budget_rejection_never_invokes_provider(
    setting, model_settings, engine, task_account, sdk
):
    model_settings = model_settings.model_copy(update={setting: Decimal("0.001")})
    with pytest.raises(BudgetExceeded):
        await generate(model_settings, engine, task_account)
    assert records(engine)[0] == []
    sdk.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, limit", [("chat", 3), ("scene", 5), ("rewrite", 5)])
async def test_completed_attempts_accumulate_cost_and_max_call_count(
    model_settings, engine, task_account, sdk, kind, limit
):
    with Session(engine) as db, db.begin():
        db.get(Task, task_account[0]).kind = kind
    for _ in range(limit):
        await generate(model_settings, engine, task_account)
    with pytest.raises(BudgetExceeded, match="Task"):
        await generate(model_settings, engine, task_account)
    assert sdk.create.await_count == limit
    calls, days = records(engine)
    assert len(calls) == limit
    assert days[0].spent_usd == Decimal("0.002000") * limit


@pytest.mark.asyncio
async def test_task_cost_includes_previous_attempts(model_settings, engine, task_account, sdk):
    model_settings = model_settings.model_copy(update={"model_task_usd": Decimal("0.05")})
    value = response()
    value.usage = SimpleNamespace(input_tokens=2000, output_tokens=200)
    sdk.create.return_value = value
    await generate(model_settings, engine, task_account)
    with pytest.raises(BudgetExceeded, match="Task"):
        await generate(model_settings, engine, task_account)
    assert sdk.create.await_count == 1


@pytest.mark.asyncio
async def test_inflight_reservation_blocks_second_provider_call(
    model_settings, engine, task_account, sdk
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        entered.set()
        await release.wait()
        return response()

    sdk.create.side_effect = provider
    first = asyncio.create_task(generate(model_settings, engine, task_account))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(ModelBusy):
            await generate(model_settings, engine, task_account)
        calls, days = records(engine)
        assert len(calls) == 1 and days[0].reserved_usd == calls[0].reserved_usd
    finally:
        release.set()
        await first
    assert sdk.create.await_count == 1


@pytest.mark.asyncio
async def test_daily_budget_counts_inflight_reservations(model_settings, engine, task_account, sdk):
    model_settings = model_settings.model_copy(
        update={
            "model_concurrency": 2,
            "model_daily_usd": Decimal("0.06"),
        }
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        entered.set()
        await release.wait()
        return response()

    sdk.create.side_effect = provider
    first = asyncio.create_task(generate(model_settings, engine, task_account))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(BudgetExceeded, match="Daily"):
            await generate(model_settings, engine, task_account)
        assert len(records(engine)[0]) == 1
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_unresolved_previous_day_call_still_occupies_global_slot(
    model_settings, engine, task_account, sdk
):
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    task_id, user_id = task_account
    with Session(engine) as db, db.begin():
        db.add(BudgetDay(day=yesterday, spent_usd=0, reserved_usd=Decimal("0.04")))
        db.flush()
        db.add(
            ModelCall(
                task_id=task_id,
                user_id=user_id,
                budget_day=yesterday,
                state="reserved",
                reserved_usd=Decimal("0.04"),
                started_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    with pytest.raises(ModelBusy):
        await generate(model_settings, engine, task_account)
    assert len(records(engine)[0]) == 1
    sdk.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected_state", [(400, "failed"), (429, "failed"), (408, "unknown"), (500, "unknown")]
)
async def test_http_failures_record_cost_without_retry(
    status, expected_state, model_settings, engine, task_account, sdk
):
    request = httpx2.Request("POST", "https://model.invalid/v1/responses")
    sdk.create.side_effect = APIStatusError(
        "synthetic rejection",
        response=httpx2.Response(status, request=request),
        body=None,
    )
    error = ModelCallFailed if expected_state == "failed" else ModelCallUnknown
    with pytest.raises(error):
        await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].state == expected_state
    assert calls[0].error == f"http_{status}"
    assert days[0].reserved_usd == 0
    if expected_state == "failed":
        assert calls[0].charged_usd == days[0].spent_usd == 0
    else:
        assert calls[0].charged_usd == days[0].spent_usd == calls[0].reserved_usd
        with pytest.raises(ModelCallUnknown):
            await generate(model_settings, engine, task_account)
    assert sdk.create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause_type,not_sent",
    [
        (httpx2.ConnectError, True),
        (httpx2.ConnectTimeout, True),
        (httpx2.PoolTimeout, True),
        (httpx2.ReadError, False),
        (httpx2.ReadTimeout, False),
        (httpx2.WriteError, False),
    ],
)
async def test_transport_cause_controls_settlement_and_logs_no_payload(
    cause_type, not_sent, model_settings, engine, task_account, sdk, caplog
):
    failure = APIConnectionError(request=httpx2.Request("POST", "https://model.invalid"))
    failure.__cause__ = cause_type("private-token-and-user-prompt")
    sdk.create.side_effect = failure
    with pytest.raises(ModelCallFailed if not_sent else ModelCallUnknown):
        await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].state == ("failed" if not_sent else "unknown")
    assert calls[0].charged_usd == (0 if not_sent else calls[0].reserved_usd)
    assert days[0].reserved_usd == 0
    assert calls[0].error == f"APIConnectionError:{cause_type.__name__}"
    assert cause_type.__name__ in caplog.text
    assert "private-token-and-user-prompt" not in caplog.text
    assert sdk.create.await_count == 1


@pytest.mark.asyncio
async def test_unknown_blocks_same_task_but_allows_new_user_request(
    model_settings, engine, task_account, sdk
):
    sdk.create.side_effect = APIConnectionError(
        request=httpx2.Request("POST", "https://model.invalid")
    )
    with pytest.raises(ModelCallUnknown):
        await generate(model_settings, engine, task_account)
    initial_calls, initial_days = records(engine)
    original_cost = initial_calls[0].charged_usd
    assert original_cost == initial_calls[0].reserved_usd == initial_days[0].spent_usd
    assert initial_days[0].reserved_usd == 0
    sdk.create.side_effect = None
    with pytest.raises(ModelCallUnknown):
        await generate(model_settings, engine, task_account)
    assert sdk.create.await_count == 1
    with Session(engine) as db, db.begin():
        task = Task(user_id=task_account[1], kind="chat", status="running")
        db.add(task)
        db.flush()
        new_id = task.id
    assert await generate(model_settings, engine, (new_id, task_account[1]))
    calls, days = records(engine)
    assert calls[0].state == "unknown" and calls[0].charged_usd == original_cost
    assert len(calls) == 2 and calls[1].state == "succeeded"
    assert days[0].spent_usd == original_cost + calls[1].charged_usd
    assert sdk.create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        response(reply="   "),
        response(status="incomplete"),
        SimpleNamespace(status="completed", output_text="not json", usage=None),
    ],
)
async def test_invalid_reply_is_not_returned_or_retried(
    value, model_settings, engine, task_account, sdk
):
    sdk.create.return_value = value
    with pytest.raises(ModelCallFailed):
        await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].state == "failed"
    assert days[0].spent_usd > 0
    assert sdk.create.await_count == 1


@pytest.mark.asyncio
async def test_missing_usage_keeps_conservative_cost(model_settings, engine, task_account, sdk):
    sdk.create.return_value = response(usage=False)
    await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].state == "succeeded"
    assert calls[0].charged_usd == calls[0].reserved_usd == days[0].spent_usd
    assert calls[0].input_tokens is None and calls[0].output_tokens is None
    assert calls[0].error == "usage_unavailable"


@pytest.mark.asyncio
async def test_cancellation_keeps_unknown_cost_and_blocks_retry(
    model_settings, engine, task_account, sdk
):
    entered = asyncio.Event()

    async def provider(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    sdk.create.side_effect = provider
    running = asyncio.create_task(generate(model_settings, engine, task_account))
    await asyncio.wait_for(entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    calls, days = records(engine)
    assert calls[0].state == "unknown" and days[0].spent_usd == calls[0].reserved_usd
    with pytest.raises(ModelCallUnknown):
        await generate(model_settings, engine, task_account)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["inactive", "finished", "wrong_user", "oversize"])
async def test_invalid_task_account_or_input_cannot_call_provider(
    condition, model_settings, engine, task_account, sdk
):
    task_id, user_id = task_account
    with Session(engine) as db, db.begin():
        if condition == "inactive":
            db.get(Account, user_id).active = False
        elif condition == "finished":
            db.get(Task, task_id).status = "succeeded"
    if condition == "wrong_user":
        user_id = uuid4()
    with pytest.raises(ModelCallFailed):
        await generate_reply(
            model_settings,
            engine,
            task_id,
            user_id,
            instructions="x" * 12_000 if condition == "oversize" else "test",
            messages=[],
        )
    assert records(engine)[0] == []
    sdk.create.assert_not_called()


@pytest.mark.asyncio
async def test_usage_settles_original_budget_day_after_midnight(
    model_settings, engine, task_account, sdk, monkeypatch
):
    today = datetime.now(UTC).date()

    class Tomorrow:
        @staticmethod
        def now(tz):
            return datetime.now(tz) + timedelta(days=1)

    async def provider(**kwargs):
        monkeypatch.setattr(model_calls, "datetime", Tomorrow)
        return response()

    sdk.create.side_effect = provider
    await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].budget_day == days[0].day == today
    assert calls[0].finished_at.astimezone(UTC).date() == today + timedelta(days=1)
    assert days[0].reserved_usd == 0 and days[0].spent_usd == Decimal("0.002000")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, state, error",
    [
        (Response.model_construct(status=None, usage=None), "unknown", ModelCallUnknown),
        (
            Response.model_construct(
                status="completed", usage=ResponseUsage.model_construct(), output=None
            ),
            "failed",
            ModelCallFailed,
        ),
    ],
)
async def test_malformed_sdk_response_is_settled_without_freeing_unknown_budget(
    value, state, error, model_settings, engine, task_account, sdk
):
    sdk.create.return_value = value
    with pytest.raises(error):
        await generate(model_settings, engine, task_account)
    calls, days = records(engine)
    assert calls[0].state == state
    assert calls[0].charged_usd == days[0].spent_usd == calls[0].reserved_usd
    assert days[0].reserved_usd == 0
    assert sdk.create.await_count == 1
