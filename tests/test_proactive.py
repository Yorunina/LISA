from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from test_chat import QQ, create_user, ingest

from lisa.chat import execute_chat
from lisa.model_calls import BudgetExceeded, ModelBusy, ModelCallUnknown
from lisa.models import Account, ChatMessage, ContactPreference, Memory, OutboundMessage, Task
from lisa.proactive import enqueue_due_contacts, execute_contact
from lisa.queue import create_queue


@pytest.fixture
def contact_settings(settings):
    return settings.model_copy(update={"proactive_enabled": True})


@pytest_asyncio.fixture
async def contact_user(db, engine, settings):
    user_id = create_user(db)
    task_id = ingest(db, "contact-origin", sent_at=datetime.now(UTC) - timedelta(hours=5))
    await execute_chat(settings, engine, task_id, generate=AsyncMock(return_value="下次聊。"))
    db.expire_all()
    reply = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    reply.status, reply.finished_at = "sent", datetime.now(UTC) - timedelta(hours=5)
    db.add(ContactPreference(user_id=user_id, enabled=True))
    db.commit()
    return user_id


def queued(db, settings):
    assert enqueue_due_contacts(db, settings) == 1
    db.commit()
    return db.scalar(select(Task).where(Task.kind == "contact"))


def test_contact_periodic_disabled_has_no_database_work(settings, monkeypatch):
    assert not settings.proactive_enabled
    connect = Mock(side_effect=AssertionError("Disabled contact must not connect"))
    monkeypatch.setattr("lisa.db.make_engine", connect)
    queue = create_queue(settings)
    assert ("lisa.contact_sweep", "") not in queue.periodic_registry.periodic_tasks
    queue.tasks["lisa.contact_sweep"].func(timestamp=0)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_contact_queue_atomic_and_one_attempt_per_feedback(
    db,
    engine,
    contact_settings,
    contact_user,
):
    task = queued(db, contact_settings)
    job = db.execute(
        text("SELECT task_name, queue_name, lock FROM procrastinate_jobs WHERE id=:id"),
        {"id": task.queue_job_id},
    ).one()
    assert (job.task_name, job.queue_name, job.lock) == (
        "lisa.contact",
        "lisa",
        f"chat:{contact_user}",
    )
    assert enqueue_due_contacts(db, contact_settings) == 0
    db.commit()
    generate = AsyncMock(return_value={"reply": "今天在窗边看了一会儿雨。"})
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    assert generate.await_count == 1
    db.expire_all()
    assert db.get(Task, task.id).status == "completed"
    assert db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id)).qq_id == QQ
    assert db.scalar(select(ChatMessage).where(ChatMessage.task_id == task.id)) is None
    assert enqueue_due_contacts(db, contact_settings) == 0


@pytest.mark.asyncio
async def test_contact_enqueue_rollback_leaves_no_business_or_queue_task(
    db,
    contact_settings,
    contact_user,
):
    before = db.scalar(text("SELECT count(*) FROM procrastinate_jobs"))
    assert enqueue_due_contacts(db, contact_settings) == 1
    db.rollback()
    assert db.scalar(select(Task).where(Task.kind == "contact")) is None
    assert db.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "inactive", "new_message", "memory"])
async def test_generation_rechecks_policy_before_model(
    db,
    engine,
    contact_settings,
    contact_user,
    change,
):
    task = queued(db, contact_settings)
    if change == "disabled":
        db.get(ContactPreference, contact_user).enabled = False
    elif change == "inactive":
        db.get(Account, contact_user).last_active_at = datetime.now(UTC) - timedelta(hours=25)
    elif change == "memory":
        db.get(Account, contact_user).memory_revision += 1
    else:
        ingest(db, "new-before-contact")
    db.commit()
    generate = AsyncMock(side_effect=AssertionError("Blocked contact must not call model"))
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    generate.assert_not_called()
    db.expire_all()
    assert db.get(Task, task.id).status == "cancelled"
    assert db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_message", "inactive", "memory"])
async def test_generation_result_is_discarded_when_policy_changes_during_call(
    db,
    engine,
    contact_settings,
    contact_user,
    change,
):
    task = queued(db, contact_settings)

    async def generate(*args):
        if change == "new_message":
            ingest(db, "during-contact", sent_at=db.get(Account, contact_user).last_active_at)
        elif change == "inactive":
            db.get(Account, contact_user).last_active_at = datetime.now(UTC) - timedelta(hours=25)
        else:
            db.get(Account, contact_user).memory_revision += 1
        db.commit()
        return {"reply": "已经过时的生活话题。"}

    await execute_contact(contact_settings, engine, task.id, generate=generate)
    db.expire_all()
    assert db.get(Task, task.id).status == "cancelled"
    assert db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id)) is None
    assert db.scalar(select(ChatMessage).where(ChatMessage.task_id == task.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BudgetExceeded, ModelBusy, ModelCallUnknown, ValueError])
async def test_contact_failure_has_no_unsolicited_error_or_automatic_retry(
    db,
    engine,
    contact_settings,
    contact_user,
    failure,
):
    task = queued(db, contact_settings)
    generate = AsyncMock(side_effect=failure("synthetic"))
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    db.expire_all()
    assert db.get(Task, task.id).status == "failed"
    assert db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id)) is None
    assert enqueue_due_contacts(db, contact_settings) == 0


@pytest.mark.asyncio
async def test_contact_context_respects_memory_permissions_and_does_not_create_user_facts(
    db,
    engine,
    contact_settings,
    contact_user,
):
    db.add_all(
        [
            Memory(
                user_id=contact_user, kind="preference", content="喜欢观察雨滴", allow_chat=True
            ),
            Memory(
                user_id=contact_user, kind="fact", content="不可用于交流的秘密", allow_chat=False
            ),
        ]
    )
    db.commit()
    task = queued(db, contact_settings)
    generate = AsyncMock(return_value={"reply": "我刚才看了一会儿雨滴。"})
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    instructions = generate.call_args.args[4]
    assert "喜欢观察雨滴" in instructions and "不可用于交流的秘密" not in instructions
    assert "不催回复" in instructions
    assert db.scalar(select(func.count()).select_from(Memory)) == 2
