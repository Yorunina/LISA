import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from procrastinate.exceptions import JobRetry
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from lisa.app import create_app
from lisa.chat import claim_outbound, execute_chat, finish_delivery, ingest_message
from lisa.model_calls import BudgetExceeded, ModelBusy, ModelCallFailed, ModelCallUnknown
from lisa.models import (
    Account,
    BudgetDay,
    ChatMessage,
    LoginSession,
    ModelCall,
    OutboundMessage,
    Persona,
    QQBinding,
    Task,
)
from lisa.queue import create_queue
from lisa.security import hash_token

QQ = "710000001"


def create_user(db, *, username="chat_reader", qq_id=QQ):
    user_id = uuid4()
    db.add(Account(id=user_id, username=username, password_hash="unused-synthetic-hash"))
    db.flush()
    db.add(QQBinding(user_id=user_id, qq_id=qq_id))
    db.add(Persona(user_id=user_id, name=username, prompt=f"{username}的独立合成性格", version=1))
    db.commit()
    return user_id


@pytest.fixture
def user_id(db):
    return create_user(db)


def ingest(db, event_id, *, qq_id=QQ, content="合成用户消息", sent_at=None, kind="text"):
    result = ingest_message(
        db,
        bot_id="910000001",
        message_id=str(event_id),
        qq_id=qq_id,
        sent_at=sent_at or datetime.now(UTC),
        content=content,
        kind=kind,
    )
    db.commit()
    return result


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_ingest_merges_persists_queue_and_does_not_refresh_replays(db, user_id):
    original_time = datetime.now(UTC) - timedelta(hours=2)
    task_id = ingest(db, 1, sent_at=original_time)
    assert ingest(db, 1, sent_at=datetime.now(UTC), content="changed replay") is None
    assert db.get(Account, user_id).last_active_at == original_time
    assert ingest(db, 2, sent_at=original_time - timedelta(hours=1)) == task_id
    assert db.get(Account, user_id).last_active_at == original_time
    image_time = datetime.now(UTC) - timedelta(minutes=1)
    assert ingest(db, 3, sent_at=image_time, kind="image", content="[图片]") == task_id
    assert db.get(Account, user_id).last_active_at == image_time
    assert count(db, Task) == 1 and count(db, ChatMessage) == 3
    task = db.get(Task, task_id)
    row = db.execute(
        text("SELECT queue_name, task_name, args, lock FROM procrastinate_jobs WHERE id=:id"),
        {"id": task.queue_job_id},
    ).one()
    assert row.queue_name == "lisa" and row.task_name == "lisa.chat"
    assert row.args == {"task_id": str(task_id)}
    assert row.lock == f"chat:{user_id}"


def test_ingest_and_queue_job_rollback_together(db, user_id):
    before = db.scalar(text("SELECT count(*) FROM procrastinate_jobs"))
    ingest_message(
        db,
        bot_id="910000001",
        message_id="1",
        qq_id=QQ,
        sent_at=datetime.now(UTC),
        content="rolled back input",
    )
    db.rollback()
    assert count(db, Task) == 0 and count(db, ChatMessage) == 0
    assert db.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == before
    assert db.get(Account, user_id).last_active_at is None


def test_concurrent_duplicate_event_creates_one_task_and_message(db, engine, user_id):
    barrier = Barrier(2)

    def receive():
        with Session(engine) as session:
            barrier.wait(timeout=10)
            return ingest(session, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: receive(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert count(db, Task) == 1 and count(db, ChatMessage) == 1


def test_uninitialized_disabled_and_future_events_do_not_queue(db, user_id):
    assert ingest(db, 1, qq_id="not-bound") is None
    assert ingest(db, 2, sent_at=datetime.now(UTC) + timedelta(minutes=2)) is None
    user = db.get(Account, user_id)
    user.active = False
    db.commit()
    assert ingest(db, 3) is None
    user.active = True
    db.delete(db.scalar(select(Persona).where(Persona.user_id == user_id)))
    db.commit()
    assert ingest(db, 4) is None
    assert count(db, Task) == 0 and db.get(Account, user_id).last_active_at is None


@pytest.mark.asyncio
async def test_bounded_batches_run_in_order_and_include_completed_reply(
    settings, engine, db, user_id
):
    first = None
    for i in range(20):
        first = ingest(db, i, content=f"first batch {i}")
    second = ingest(db, 20, content="second batch")
    assert first != second
    calls = []

    async def generate(*args):
        calls.append(args[-1])
        return f"synthetic reply {len(calls)}"

    with pytest.raises(JobRetry):
        await execute_chat(settings, engine, str(second), generate=generate)
    assert not calls
    await execute_chat(settings, engine, str(first), generate=generate)
    assert all("second batch" not in message["content"] for message in calls[0])
    await execute_chat(settings, engine, str(second), generate=generate)
    assert any(message["content"] == "synthetic reply 1" for message in calls[1])
    assert calls[1][-1]["content"].endswith("second batch")
    assert count(db, OutboundMessage) == 2


@pytest.mark.asyncio
async def test_long_old_message_does_not_block_new_chat_and_latest_history_is_kept(
    settings, engine, db, user_id
):
    calls = []

    async def generate(settings, engine, task_id, account_id, instructions, messages):
        calls.append((instructions, messages))
        return "最新历史回答"

    old_content = "旧" * 4000
    old_task = ingest(db, 1, content=old_content)
    await execute_chat(settings, engine, str(old_task), generate=generate)
    db.expire_all()
    assert db.get(Task, old_task).error == "input_too_large"
    assert not calls

    recent = ingest(db, 2, content="最新历史问题")
    await execute_chat(settings, engine, str(recent), generate=generate)
    current = ingest(db, 3, content="当前批次第一条")
    assert ingest(db, 4, content="当前批次第二条") == current
    await execute_chat(settings, engine, str(current), generate=generate)

    instructions, messages = calls[-1]
    assert "chat_reader的独立合成性格" in instructions
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "user"]
    assert messages[0]["content"].endswith("最新历史问题")
    assert messages[1]["content"] == "最新历史回答"
    assert messages[2]["content"].endswith("当前批次第一条")
    assert messages[3]["content"].endswith("当前批次第二条")
    db.expire_all()
    assert db.get(Task, current).status == "completed"
    assert (
        db.scalar(select(ChatMessage.content).where(ChatMessage.task_id == old_task)) == old_content
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["persona", "batch"])
async def test_persona_and_current_batch_are_never_silently_truncated(
    settings, engine, db, user_id, oversized
):
    persona = db.scalar(select(Persona).where(Persona.user_id == user_id))
    if oversized == "persona":
        persona.prompt = "人格" * 6000
        db.commit()
    first = "甲" * 2000 if oversized == "batch" else "短消息"
    second = "乙" * 2000 if oversized == "batch" else "另一条短消息"
    task_id = ingest(db, 1, content=first)
    assert ingest(db, 2, content=second) == task_id

    async def must_not_generate(*args):
        pytest.fail("Oversized current input was silently shortened and sent to the model")

    await execute_chat(settings, engine, str(task_id), generate=must_not_generate)
    db.expire_all()
    assert db.get(Task, task_id).error == "input_too_large"
    assert db.get(Task, task_id).status == "failed"
    assert set(db.scalars(select(ChatMessage.content).where(ChatMessage.task_id == task_id))) == {
        first,
        second,
    }
    notice = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert "缩短性格设定" in notice.content and "分段发送" in notice.content
    if oversized == "persona":
        assert persona.prompt == "人格" * 6000
    assert count(db, ModelCall) == 0


@pytest.mark.asyncio
async def test_new_message_during_generation_gets_next_task_and_other_user_runs(
    settings, engine, db, user_id
):
    other_id = create_user(db, username="other_reader", qq_id="710000002")
    first = ingest(db, 1, content="first user's initial input")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def generate(settings, engine, task_id, account_id, instructions, messages):
        calls.append((account_id, instructions, messages))
        if task_id == first:
            started.set()
            await release.wait()
        return f"reply for {account_id}"

    running = asyncio.create_task(execute_chat(settings, engine, str(first), generate=generate))
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = ingest(db, 2, content="first user's follow up")
        assert first != second
        other_task = ingest(db, 3, qq_id="710000002", content="other user's private input")
        await execute_chat(settings, engine, str(other_task), generate=generate)
        assert calls[-1][0] == other_id
        assert "other_reader的独立合成性格" in calls[-1][1]
        assert all("first user's" not in message["content"] for message in calls[-1][2])
        with pytest.raises(JobRetry):
            await execute_chat(settings, engine, str(second), generate=generate)
    finally:
        release.set()
        await running
    await execute_chat(settings, engine, str(second), generate=generate)
    assert calls[-1][0] == user_id
    assert any(message["content"] == f"reply for {user_id}" for message in calls[-1][2])
    assert all("other user's" not in message["content"] for message in calls[-1][2])


@pytest.mark.asyncio
async def test_message_time_order_and_completed_task_are_not_regenerated(
    settings, engine, db, user_id
):
    now = datetime.now(UTC)
    task_id = ingest(db, 1, content="newer", sent_at=now)
    ingest(db, 2, content="older", sent_at=now - timedelta(days=2))
    calls = []

    async def generate(*args):
        calls.append(args[-1])
        return "one reply"

    await execute_chat(settings, engine, str(task_id), generate=generate)
    await execute_chat(settings, engine, str(task_id), generate=generate)
    assert len(calls) == 1
    assert calls[0][0]["content"].endswith("older")
    assert "消息时间" in calls[0][0]["content"]
    assert calls[0][1]["content"].endswith("newer")
    db.expire_all()
    assert db.get(Account, user_id).last_active_at == now
    assert count(db, OutboundMessage) == 1
    assert (
        db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.role == "assistant")
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_during_call", [False, True])
async def test_disabled_account_cannot_start_or_accept_reply(
    settings, engine, db, user_id, disable_during_call
):
    task_id = ingest(db, 1)
    calls = []

    async def generate(*args):
        calls.append(1)
        with Session(engine) as session:
            session.get(Account, user_id).active = False
            session.commit()
        return "late reply must not be saved"

    if not disable_during_call:
        db.get(Account, user_id).active = False
        db.commit()
    await execute_chat(settings, engine, str(task_id), generate=generate)
    db.expire_all()
    assert len(calls) == int(disable_during_call)
    assert db.get(Task, task_id).status == "failed"
    assert count(db, OutboundMessage) == 0
    assert count(db, ChatMessage) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (BudgetExceeded, "budget_exhausted"),
        (ModelCallFailed, "model_failed"),
        (ModelCallUnknown, "model_unknown"),
    ],
)
async def test_model_failures_are_fixed_notices_not_assistant_history(
    settings, engine, db, user_id, failure, error
):
    task_id = ingest(db, 1)
    calls = []

    async def generate(*args):
        calls.append(1)
        raise failure()

    await execute_chat(settings, engine, str(task_id), generate=generate)
    await execute_chat(settings, engine, str(task_id), generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == error
    assert len(calls) == 1
    assert count(db, ChatMessage) == 1
    assert count(db, OutboundMessage) == 1


@pytest.mark.asyncio
async def test_busy_model_retries_same_task_but_stops_after_five_minutes(
    settings, engine, db, user_id
):
    task_id = ingest(db, 1)

    async def busy(*args):
        raise ModelBusy()

    with pytest.raises(JobRetry):
        await execute_chat(settings, engine, str(task_id), generate=busy)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "pending"
    assert count(db, Task) == 1 and count(db, OutboundMessage) == 0
    task.created_at = datetime.now(UTC) - timedelta(minutes=6)
    db.commit()
    await execute_chat(settings, engine, str(task_id), generate=busy)
    db.expire_all()
    assert db.get(Task, task_id).error == "model_busy"
    assert count(db, ChatMessage) == 1 and count(db, OutboundMessage) == 1


@pytest.mark.asyncio
async def test_persisted_running_task_is_not_blindly_restarted(settings, engine, db, user_id):
    task_id = ingest(db, 1)
    db.get(Task, task_id).status = "running"
    db.commit()

    async def must_not_generate(*args):
        pytest.fail("Unknown interrupted task was automatically regenerated")

    await execute_chat(settings, engine, str(task_id), generate=must_not_generate)
    assert count(db, OutboundMessage) == 0


def test_real_queue_job_executes_registered_chat_task(settings, engine, db, user_id, monkeypatch):
    from lisa import chat

    task_id = ingest(db, 1)
    calls = []

    async def fake_reply(*args):
        calls.append(args[2])
        return "queued synthetic reply"

    monkeypatch.setattr(chat, "generate_conversation", fake_reply)
    task = db.get(Task, task_id)
    job_id = task.queue_job_id
    db.execute(
        text("UPDATE procrastinate_jobs SET scheduled_at=now() WHERE id=:id"), {"id": job_id}
    )
    db.commit()

    run_chat_worker(settings)
    assert calls == [task_id]
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert (
        db.scalar(text("SELECT status FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
        == "succeeded"
    )


def run_chat_worker(settings):
    async def run():
        queue = create_queue(settings)
        async with queue.open_async():
            await asyncio.wait_for(
                queue.run_worker_async(
                    queues=["lisa"],
                    wait=False,
                    concurrency=2,
                    install_signal_handlers=False,
                ),
                timeout=10,
            )

    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(run())


def make_job_due(db, job_id):
    db.execute(
        text("UPDATE procrastinate_jobs SET scheduled_at=now() WHERE id=:id"), {"id": job_id}
    )
    db.commit()


def test_worker_reschedules_model_busy_then_completes_same_job(settings, db, user_id, monkeypatch):
    from lisa import chat

    task_id = ingest(db, 1)
    job_id = db.get(Task, task_id).queue_job_id
    calls = []

    async def fake_reply(*args):
        calls.append(args[2])
        if len(calls) == 1:
            raise ModelBusy()
        return "reply after the busy slot was released"

    monkeypatch.setattr(chat, "generate_conversation", fake_reply)
    make_job_due(db, job_id)
    started = datetime.now(UTC)
    run_chat_worker(settings)
    db.expire_all()
    assert db.get(Task, task_id).status == "pending"
    retry = db.execute(
        text("SELECT status, attempts, scheduled_at FROM procrastinate_jobs WHERE id=:id"),
        {"id": job_id},
    ).one()
    assert retry.status == "todo"
    assert retry.attempts == 1
    assert retry.scheduled_at >= started + timedelta(seconds=2)
    assert calls == [task_id] and count(db, OutboundMessage) == 0
    make_job_due(db, job_id)
    run_chat_worker(settings)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed" and task.queue_job_id == job_id
    assert (
        db.scalar(text("SELECT status FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
        == "succeeded"
    )
    assert calls == [task_id, task_id]
    assert count(db, Task) == 1 and count(db, OutboundMessage) == 1
    run_chat_worker(settings)
    assert calls == [task_id, task_id]


@pytest.mark.parametrize(
    "failure,job_status,task_status",
    [(ModelCallUnknown, "succeeded", "failed"), (RuntimeError, "failed", "running")],
)
def test_worker_does_not_retry_unknown_results_or_unexpected_errors(
    settings, db, user_id, monkeypatch, failure, job_status, task_status
):
    from lisa import chat

    task_id = ingest(db, 1)
    job_id = db.get(Task, task_id).queue_job_id
    calls = []

    async def fake_reply(*args):
        calls.append(args[2])
        raise failure("synthetic failure")

    monkeypatch.setattr(chat, "generate_conversation", fake_reply)
    make_job_due(db, job_id)
    run_chat_worker(settings)
    db.expire_all()
    assert db.get(Task, task_id).status == task_status
    job = db.execute(
        text("SELECT status, attempts FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}
    ).one()
    assert job.status == job_status and job.attempts == 1
    run_chat_worker(settings)
    assert calls == [task_id]


def seed_outbound(db, user_id, *, content="persisted reply", qq_id=QQ):
    task = Task(user_id=user_id, status="completed")
    db.add(task)
    db.flush()
    message = OutboundMessage(user_id=user_id, task_id=task.id, qq_id=qq_id, content=content)
    db.add(message)
    db.flush()
    message_id = message.id
    db.commit()
    return message_id


def test_explicit_delivery_failure_retries_same_payload_at_most_three_times(db, user_id):
    message_id = seed_outbound(db, user_id)
    for attempt in range(1, 4):
        message = claim_outbound(db, QQ)
        assert message.id == message_id and message.content == "persisted reply"
        assert message.attempts == attempt and message.status == "sending"
        db.commit()
        assert claim_outbound(db, QQ) is None
        finish_delivery(db, message_id, "failed")
        db.commit()
        if attempt < 3:
            assert claim_outbound(db, QQ) is None
            message.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
    assert db.get(OutboundMessage, message_id).status == "failed"
    assert claim_outbound(db, QQ) is None
    assert count(db, OutboundMessage) == 1 and count(db, Task) == 1


@pytest.mark.parametrize("status", ["sent", "unknown"])
def test_sent_and_unknown_delivery_are_final_and_repeated_results_do_not_resend(
    db, user_id, status
):
    message_id = seed_outbound(db, user_id)
    assert claim_outbound(db, "other-qq") is None
    assert claim_outbound(db, QQ).id == message_id
    db.commit()
    finish_delivery(db, message_id, status, gateway_id="123" if status == "sent" else None)
    db.commit()
    finish_delivery(db, message_id, "failed")
    db.commit()
    assert claim_outbound(db, QQ) is None
    message = db.get(OutboundMessage, message_id)
    assert message.status == status and message.attempts == 1


def test_disabled_account_cannot_send_saved_reply(db, user_id):
    message_id = seed_outbound(db, user_id)
    db.get(Account, user_id).active = False
    db.commit()
    assert claim_outbound(db, QQ) is None
    db.commit()
    assert db.get(OutboundMessage, message_id).status == "cancelled"


def test_reply_order_is_per_user_and_other_users_can_send_during_retry_delay(db, user_id):
    first = seed_outbound(db, user_id, content="first reply")
    second = seed_outbound(db, user_id, content="second reply")
    other_id = create_user(db, username="other_outbound", qq_id="710000002")
    other = seed_outbound(db, other_id, content="other user's reply", qq_id="710000002")
    assert claim_outbound(db, None).id == first
    db.commit()
    assert claim_outbound(db, QQ) is None
    finish_delivery(db, first, "failed")
    db.commit()
    assert claim_outbound(db, QQ) is None
    assert claim_outbound(db, None).id == other
    db.commit()
    finish_delivery(db, other, "sent", gateway_id="other-delivery")
    db.commit()
    assert claim_outbound(db, None) is None
    db.get(OutboundMessage, first).next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert claim_outbound(db, None).id == first
    db.commit()
    finish_delivery(db, first, "sent", gateway_id="123")
    db.commit()
    # An account's saved reply must not follow a changed binding to a different QQ.
    db.get(QQBinding, user_id).qq_id = "710000003"
    db.commit()
    assert claim_outbound(db, None) is None
    assert db.get(OutboundMessage, second).status == "cancelled"


@pytest.mark.parametrize(
    "enabled,key,expected",
    [
        (False, "synthetic-key", "unavailable"),
        (True, "", "unavailable"),
        (True, "synthetic-key", "ready"),
    ],
)
def test_service_status_requires_login_and_actual_configuration(
    settings, db, user_id, enabled, key, expected
):
    configured = settings.model_copy(
        update={
            "chat_enabled": enabled,
            "chat_test_enabled": False,
            "model_api_key": SecretStr(key),
        }
    )
    db.add(
        LoginSession(
            token_hash=hash_token("synthetic-status-session"),
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.commit()
    with TestClient(create_app(configured)) as client:
        assert client.get("/api/service-status").status_code == 401
        client.cookies.set("lisa_session", "synthetic-status-session")
        response = client.get("/api/service-status")
        assert response.json()["status"] == expected
        assert set(response.json()) == {"status", "message"}
        assert "synthetic-key" not in response.text


def test_service_status_reports_budget_but_old_unknown_does_not_block_other_users(
    settings, db, user_id
):
    configured = settings.model_copy(
        update={
            "chat_test_enabled": True,
            "model_api_key": SecretStr("synthetic-key"),
            "model_daily_usd": Decimal("1"),
        }
    )
    db.add(
        LoginSession(
            token_hash=hash_token("synthetic-status-session"),
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    day = BudgetDay(day=datetime.now(UTC).date(), spent_usd=Decimal("1"), reserved_usd=0)
    db.add(day)
    db.commit()
    with TestClient(create_app(configured)) as client:
        client.cookies.set("lisa_session", "synthetic-status-session")
        assert client.get("/api/service-status").json()["status"] == "budget_exhausted"
        day.spent_usd = Decimal("0")
        other_id = create_user(db, username="private_other_account", qq_id="710000002")
        task = Task(user_id=other_id, status="failed")
        db.add(task)
        db.flush()
        db.add(
            ModelCall(
                task_id=task.id,
                user_id=other_id,
                budget_day=day.day,
                state="unknown",
                reserved_usd=Decimal("0.01"),
            )
        )
        db.commit()
        response = client.get("/api/service-status")
        assert response.json()["status"] == "ready"
        assert set(response.json()) == {"status", "message"}
        assert str(other_id) not in response.text and "private_other_account" not in response.text
