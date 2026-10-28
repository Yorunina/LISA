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
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    generate_reply,
    trim_history,
)
from lisa.models import Account, BudgetDay, ModelCall, Task
from lisa.settings import Settings
from lisa.structured_output import structured_schema


@pytest.fixture
def model_settings(settings):
    return settings.model_copy(
        update={
            "model_reasoning_effort": None,
            "model_base_url": "https://model.invalid/v1",
            "model_api_key": SecretStr("synthetic-key"),
            "model_name": "gpt-6-astra",
            "model_api_protocol": "responses",
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
    task = Task(user_id=user.id, kind="model_probe", source="development", status="running")
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


def test_chat_commission_interpretation_keeps_billing_without_consumption_caps(
    model_settings, engine, db, task_account
):
    task_id, user_id = task_account
    task = db.get(Task, task_id)
    task.kind = "chat"
    db.add(BudgetDay(day=datetime.now(UTC).date(), spent_usd=Decimal("999")))
    db.commit()
    for _ in range(model_calls.MAX_TASK_CALLS + 1):
        call_id = model_calls._reserve(
            model_settings, engine, task_id, user_id, Decimal("1")
        )
        model_calls._finish(engine, call_id, state="succeeded", charged=Decimal("1"))
    db.expire_all()
    calls = list(db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)))
    assert len(calls) == model_calls.MAX_TASK_CALLS + 1
    assert all(not call.budget_exempt and call.charged_usd == 1 for call in calls)
    assert db.scalar(select(BudgetDay.spent_usd)) == Decimal("999") + len(calls)


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
    chat_create = AsyncMock()

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.responses = SimpleNamespace(create=create)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=chat_create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(model_calls, "AsyncOpenAI", FakeClient)
    return SimpleNamespace(create=create, chat_create=chat_create, configurations=calls)


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
@pytest.mark.parametrize("model_name", ["gpt-6-astra", "gpt-6.1-sol"])
async def test_reservation_precedes_request_and_usage_settles_cost(
    model_settings, engine, task_account, sdk, model_name
):
    model_settings = model_settings.model_copy(update={"model_name": model_name})
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
        "model": model_name,
        "base_url": "https://model.invalid/v1",
        "max_output_tokens": 600,
        "input_per_million": "10",
        "output_per_million": "50",
        "request_bytes": 400,
        "input_limit_bytes": 64000,
        "protocol": "responses",
        "schema": "Reply",
        "schema_fingerprint": structured_schema(model_calls.Reply).fingerprint,
        "reasoning_effort": "low",
    }
    assert days[0].spent_usd == calls[0].charged_usd
    assert days[0].reserved_usd == 0
    assert sdk.configurations[0]["max_retries"] == 0
    assert sdk.configurations[0]["timeout"] == 60
    assert sdk.configurations[0]["http_client"].follow_redirects is False


@pytest.mark.asyncio
async def test_explicit_reasoning_effort_reaches_relay(
    model_settings, engine, task_account, sdk
):
    configured = model_settings.model_copy(update={
        "model_name": "deepseek-v4.1-flash", "model_reasoning_effort": "high",
    })
    sdk.create.return_value = response()
    await generate(configured, engine, task_account)
    request = sdk.create.call_args.kwargs
    assert request["model"] == "deepseek-v4.1-flash"
    assert request["reasoning"] == {"effort": "high"}
    assert request["text"]["format"]["strict"] is True
    assert records(engine)[0][0].configuration["request_bytes"] == model_calls.request_size(request)


def test_reasoning_effort_rejects_typos_and_accepts_empty_override(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("LISA_MODEL_REASONING_EFFORT", "")
    assert Settings().model_reasoning_effort is None
    monkeypatch.setenv("LISA_MODEL_REASONING_EFFORT", "hig")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.asyncio
async def test_history_trimming_uses_actual_request_size_and_keeps_current_input(
    model_settings, engine, task_account, sdk
):
    model_settings = model_settings.model_copy(update={"model_max_input_bytes": 11_000})
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
        <= model_settings.model_max_input_bytes
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
@pytest.mark.parametrize("kind, limit", [("model_probe", 3), ("scene", 5), ("rewrite", 5)])
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
    "status, expected_state",
    [(400, "failed"), (429, "failed"), (408, "unknown"), (500, "unknown"), (502, "unknown")],
)
async def test_http_failures_record_cost_without_retry(
    status, expected_state, model_settings, engine, task_account, sdk, caplog
):
    request = httpx2.Request("POST", "https://model.invalid/v1/responses")
    sdk.create.side_effect = APIStatusError(
        "private-token-and-user-prompt",
        response=httpx2.Response(
            status, request=request, headers={"x-request-id": "synthetic-request-id"}
        ),
        body={"private": "private-token-and-user-prompt"},
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
    assert "request_id=synthetic-request-id" in caplog.text
    assert "private-token-and-user-prompt" not in caplog.text


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
@pytest.mark.parametrize("creative", [False, True])
async def test_chat_wire_contract_survives_prose_persona(
    creative, model_settings, engine, task_account, sdk
):
    from lisa.dialogue import (
        Conversation,
        Dialogue,
        generate_conversation,
        generate_dialogue,
    )

    schema = Dialogue if creative else Conversation
    expected = Dialogue(reply="合成回复", opinions=[], commission=None) if creative \
        else Conversation(reply="合成回复")
    sdk.create.return_value = SimpleNamespace(
        status="completed", output_text=expected.model_dump_json(), usage=None,
    )
    generator = generate_dialogue if creative else generate_conversation
    persona = "只输出发给对方的普通文字，不要解释。"
    actual = await generator(
        model_settings, engine, *task_account, persona,
        [{"role": "user", "content": "在做什么"}],
    )
    assert actual == expected
    request = sdk.create.call_args.kwargs
    assert request["instructions"].startswith(persona)
    assert request["input"][0]["role"] == "developer"
    assert "JSON传输协议" in request["input"][0]["content"]
    shape = json.loads(request["input"][0]["content"].rsplit("\n", 1)[1])
    assert request["input"][-1] == {"role": "user", "content": "在做什么"}
    assert set(shape) == set(request["text"]["format"]["schema"]["required"])
    schema.model_validate(shape)
    assert records(engine)[0][0].configuration["request_bytes"] == model_calls.request_size(request)
    assert sdk.create.await_count == 1


def test_chat_wire_contract_is_included_in_context_input_limit(model_settings):
    from lisa.dialogue import Dialogue

    messages = [{"role": "user", "content": "合成消息"}]
    instructions = "自然交流。"
    request = model_calls._reply_request(model_settings, instructions, messages, Dialogue)
    assert "JSON传输协议" in request["input"][0]["content"]
    limit = model_calls.request_size(request) - 1
    bounded = model_settings.model_copy(update={"model_max_input_bytes": limit})
    with pytest.raises(model_calls.ModelInputTooLarge):
        trim_history(bounded, instructions, messages, 0, schema=Dialogue)


def completion_response(content, *, reason="stop", usage=True, refusal=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=reason,
            message=SimpleNamespace(content=content, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=80) if usage else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("creative", [False, True])
async def test_explicit_chat_completions_uses_json_mode_and_shared_ledger(
    creative, model_settings, engine, task_account, sdk
):
    from lisa.dialogue import (
        Conversation,
        Dialogue,
        generate_conversation,
        generate_dialogue,
    )

    configured = model_settings.model_copy(update={
        "model_api_protocol": "chat_completions", "model_reasoning_effort": "high",
    })
    expected = Dialogue(reply="合成回复", opinions=[], commission=None) if creative \
        else Conversation(reply="合成回复")
    sdk.chat_create.return_value = completion_response(expected.model_dump_json())
    generator = generate_dialogue if creative else generate_conversation
    actual = await generator(configured, engine, *task_account, "合成性格。",
                             [{"role": "user", "content": "你好"}])
    assert actual == expected
    request = sdk.chat_create.call_args.kwargs
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 600 and request["reasoning_effort"] == "high"
    assert request["store"] is False
    assert request["messages"][0]["role"] == "system"
    assert "JSON Schema" in request["messages"][0]["content"]
    assert request["messages"][-1] == {"role": "user", "content": "你好"}
    calls, days = records(engine)
    assert calls[0].configuration["protocol"] == "chat_completions"
    assert calls[0].configuration["request_bytes"] == model_calls.request_size(request)
    assert calls[0].state == "succeeded" and calls[0].input_tokens == 120
    assert calls[0].output_tokens == 80 and days[0].reserved_usd == 0
    assert calls[0].charged_usd == days[0].spent_usd
    sdk.create.assert_not_called()
    assert sdk.chat_create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,content,refusal,exception,state", [
    ("stop", "not json", None, ModelCallFailed, "failed"),
    ("length", '{"reply":"truncated', None, ModelCallFailed, "failed"),
    ("content_filter", "", None, ModelCallFailed, "failed"),
    ("stop", '{"reply":"unsafe"}', "rejected", ModelCallFailed, "failed"),
    (None, "", None, ModelCallUnknown, "unknown"),
])
async def test_chat_completions_failures_never_fallback_or_replay(
    reason, content, refusal, exception, state, model_settings, engine, task_account, sdk
):
    from lisa.dialogue import generate_conversation

    configured = model_settings.model_copy(update={"model_api_protocol": "chat_completions"})
    sdk.chat_create.return_value = completion_response(content, reason=reason, refusal=refusal)
    with pytest.raises(exception):
        await generate_conversation(configured, engine, *task_account, "合成性格",
                                    [{"role": "user", "content": "合成消息"}])
    assert records(engine)[0][0].state == state
    sdk.create.assert_not_called()
    assert sdk.chat_create.await_count == 1


@pytest.mark.asyncio
async def test_chat_completions_also_applies_to_non_chat_schemas(
    model_settings, engine, task_account, sdk
):
    configured = model_settings.model_copy(update={"model_api_protocol": "chat_completions"})
    sdk.chat_create.return_value = completion_response('{"reply":"可以，我们慢慢写。"}')
    assert await generate(configured, engine, task_account) == "可以，我们慢慢写。"
    assert sdk.chat_create.await_count == 1
    sdk.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"reply":"PRIVATE_REPLY_SENTINEL","PRIVATE_FIELD_SENTINEL":"SECRET_VALUE"}',
    'PRIVATE_NON_JSON_SENTINEL',
])
async def test_invalid_reply_diagnostics_never_log_private_output(
    raw, model_settings, engine, task_account, sdk, caplog
):
    from lisa.dialogue import Dialogue

    sdk.create.return_value = SimpleNamespace(status="completed", output_text=raw, usage=None)
    with caplog.at_level("WARNING", logger="lisa.model_calls"):
        with pytest.raises(ModelCallFailed):
            await model_calls.generate_structured(
                model_settings, engine, *task_account, "合成测试",
                [{"role": "user", "content": "合成消息"}], schema=Dialogue,
            )
    assert "invalid_reply schema=Dialogue" in caplog.text
    if raw.startswith("{"):
        assert "opinions" in caplog.text and "commission" in caplog.text
        assert "extra_forbidden" in caplog.text
    else:
        assert "json_invalid" in caplog.text
    assert "PRIVATE" not in caplog.text and "SECRET_VALUE" not in caplog.text
    assert records(engine)[0][0].error == (
        "output_missing_fields" if raw.startswith("{") else "output_json_invalid"
    )
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
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("failure,expected", [
    ("missing_top", "output_missing_fields"),
    ("missing_nested", "output_missing_fields"),
    ("scalar_type", "output_type_invalid"),
    ("constraint", "output_constraint_invalid"),
    ("extra", "output_extra_fields"),
])
async def test_invalid_wire_results_are_paid_failures_without_repair_or_retry(
    protocol, failure, expected, model_settings, engine, task_account, sdk,
):
    from lisa.dialogue import Dialogue

    value = Dialogue(
        reply="synthetic", opinions=[], commission=None,
        memories=[{"content": "synthetic", "kind": "fact", "evidence": "synthetic"}],
    ).model_dump(mode="json")
    if failure == "missing_top":
        value.pop("scene_plan")
    elif failure == "missing_nested":
        value["memories"][0].pop("basis")
    elif failure == "scalar_type":
        value["message_intervals"] = ["1.0"]
    elif failure == "constraint":
        value["reply"] = " "
    else:
        value["unknown_private_field"] = "secret"
    raw = json.dumps(value)
    sdk.create.return_value = SimpleNamespace(
        status="completed", output_text=raw,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    sdk.chat_create.return_value = completion_response(raw)
    cfg = model_settings.model_copy(update={"model_api_protocol": protocol})
    with pytest.raises(ModelCallFailed):
        await model_calls.generate_structured(
            cfg, engine, *task_account, "synthetic", [], schema=Dialogue,
        )
    calls, days = records(engine)
    assert len(calls) == 1 and calls[0].state == "failed" and calls[0].error == expected
    assert calls[0].charged_usd > 0 and days[0].reserved_usd == 0
    assert sdk.create.await_count + sdk.chat_create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason,refusal,expected", [
    ("completed", None, True, "model_refusal"),
    ("incomplete", "max_output_tokens", False, "output_limit"),
    ("incomplete", "content_filter", False, "content_filtered"),
    ("incomplete", "PRIVATE_PROVIDER_DETAIL", False, "response_incomplete"),
    ("failed", None, False, "provider_failed"),
    ("cancelled", None, False, "response_cancelled"),
    ({"private": "detail"}, None, False, "response_status_unknown"),
])
async def test_responses_terminal_reasons_are_classified_without_exposing_content(
    status, reason, refusal, expected, model_settings, engine, task_account, sdk, caplog,
):
    value = response(status=status)
    value.incomplete_details = SimpleNamespace(reason=reason)
    value.output = [SimpleNamespace(content=[SimpleNamespace(
        type="refusal", refusal="PRIVATE_REFUSAL_DETAIL",
    )])] if refusal else []
    sdk.create.return_value = value
    exception = ModelCallUnknown if not isinstance(status, str) else ModelCallFailed
    with pytest.raises(exception):
        await generate(model_settings, engine, task_account)
    call = records(engine)[0][0]
    assert call.error == expected
    assert call.state == ("unknown" if exception is ModelCallUnknown else "failed")
    assert call.charged_usd == (call.reserved_usd if exception is ModelCallUnknown
                                else Decimal("0.002000"))
    assert "PRIVATE" not in caplog.text
    assert sdk.create.await_count == 1


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
    if condition == "oversize":
        model_settings = model_settings.model_copy(update={"model_max_input_bytes": 11_000})
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
