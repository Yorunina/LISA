"""Contact delivery uses the real outbox/policy; only the QQ transport is mocked."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_chat import QQ, create_user, ingest

from lisa import gateway
from lisa.chat import claim_outbound, finish_delivery
from lisa.contact_policy import contact_snapshot
from lisa.life import queue_life_shares
from lisa.models import Account, ChatMessage, ContactPreference, Memory, OutboundMessage, Task


def seed_contact(db):
    user_id = create_user(db)
    anchor_task_id = ingest(db, 700, sent_at=datetime.now(UTC) - timedelta(hours=2))
    anchor = db.get(Task, anchor_task_id)
    anchor.status, anchor.finished_at = "completed", datetime.now(UTC)
    db.add(ContactPreference(user_id=user_id, enabled=True, min_interval_minutes=60))
    db.flush()
    task = Task(
        user_id=user_id,
        kind="contact",
        source="autonomous",
        status="completed",
        request=contact_snapshot(db, user_id),
        input_snapshot={
            "life_events": [
                {
                    "content": "今天在窗边读了一篇短诗",
                    "nature": "event",
                    "occurred_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                    "evidence": "在窗边读了一篇短诗",
                }
            ]
        },
    )
    db.add(task)
    db.flush()
    memory = Memory(
        user_id=user_id,
        kind="life",
        content="昨天散步看见银杏叶",
        life_kind="event",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
        allow_chat=True,
    )
    db.add(memory)
    db.flush()
    queue_life_shares(db, user_id, task.id, [memory.id])
    outbound = OutboundMessage(
        user_id=user_id,
        task_id=task.id,
        qq_id=QQ,
        content="想起昨天的银杏叶，今天又在窗边读了一篇短诗。",
        status="pending",
    )
    db.add(outbound)
    db.commit()
    return user_id, task.id, outbound.id, memory.id


async def send_once(engine, settings, monkeypatch, *, outcome="sent", before_result=None):
    finished = asyncio.Event()
    original_finish = gateway.finish_delivery
    calls = []

    class Transport:
        async def call(self, action, params=None):
            calls.append((action, params))
            assert action == "send_private_msg"
            if before_result:
                before_result()
            if outcome == "unknown":
                raise gateway.OneBotUnknown("synthetic missing acknowledgement")
            if outcome == "failed":
                raise gateway.OneBotFailure("synthetic rejection")
            return {"message_id": 8123}

    def finish_and_signal(*args, **kwargs):
        original_finish(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(gateway, "finish_delivery", finish_and_signal)
    sender = asyncio.create_task(gateway.deliver_outbox(Transport(), engine, QQ, settings=settings))
    try:
        await asyncio.wait_for(finished.wait(), 3)
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed"])
async def test_contact_delivery_adopts_life_without_history_only_after_confirmed_send(
    db, engine, settings, monkeypatch, outcome
):
    user_id, task_id, outbound_id, memory_id = seed_contact(db)
    config = settings.model_copy(update={"proactive_enabled": True})
    assert db.scalar(select(ChatMessage.id).where(ChatMessage.task_id == task_id)) is None
    expected_content = db.get(OutboundMessage, outbound_id).content
    calls = await send_once(engine, config, monkeypatch, outcome=outcome)
    db.expire_all()
    outbound = db.get(OutboundMessage, outbound_id)
    assert outbound.status == {"sent": "sent", "unknown": "unknown", "failed": "pending"}[outcome]
    assert outbound.attempts == 1
    assert len(calls) == 1
    assert calls[0] == (
        "send_private_msg",
        {
            "user_id": int(QQ),
            "message": [{"type": "text", "data": {"text": expected_content}}],
        },
    )
    sources = list(db.scalars(select(ChatMessage).where(ChatMessage.task_id == task_id)))
    new_life = db.scalar(select(Memory).where(Memory.content == "今天在窗边读了一篇短诗"))
    mentioned = db.get(Memory, memory_id)
    if outcome == "sent":
        assert sources == [] and outbound.content == ""
        assert new_life.source_message_id is None and new_life.source_task_id == task_id
        assert new_life.shared_at == mentioned.shared_at == outbound.finished_at
        finish_delivery(db, outbound_id, "sent", "duplicate acknowledgement")
        db.commit()
        assert db.scalar(select(ChatMessage.id).where(ChatMessage.task_id == task_id)) is None
        assert list(db.scalars(select(Memory.id).where(Memory.source_task_id == task_id))) == [
            new_life.id
        ]
    else:
        assert sources == [] and new_life is None and mentioned.shared_at is None
        if outcome == "unknown":
            assert outbound.content == ""
            assert claim_outbound(db, QQ, settings=config) is None
        else:
            # Only the bounded pending retry retains its transient payload.
            assert outbound.content == expected_content
            # Known transport failure keeps one outbox, but a later opt-out prevents retry.
            outbound.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            db.get(ContactPreference, user_id).enabled = False
            db.commit()
            assert claim_outbound(db, QQ, settings=config) is None
            assert outbound.status == "cancelled"


@pytest.mark.parametrize("change", ["memory", "cancelled"])
def test_contact_rechecks_memory_and_task_cancellation_before_claim(db, settings, change):
    user_id, task_id, outbound_id, _ = seed_contact(db)
    if change == "memory":
        db.get(Account, user_id).memory_revision += 1
    else:
        db.get(Task, task_id).status = "cancelled"
    db.commit()
    assert (
        claim_outbound(db, QQ, settings=settings.model_copy(update={"proactive_enabled": True}))
        is None
    )
    assert db.get(OutboundMessage, outbound_id).status == "cancelled"
    assert db.scalar(select(ChatMessage.id).where(ChatMessage.task_id == task_id)) is None


@pytest.mark.asyncio
async def test_memory_edit_while_contact_is_sending_cannot_adopt_stale_life(
    db, engine, settings, monkeypatch
):
    user_id, task_id, outbound_id, _ = seed_contact(db)

    def edit_memory():
        with Session(engine) as session, session.begin():
            session.get(Account, user_id).memory_revision += 1

    await send_once(
        engine,
        settings.model_copy(update={"proactive_enabled": True}),
        monkeypatch,
        before_result=edit_memory,
    )
    db.expire_all()
    assert db.get(OutboundMessage, outbound_id).status == "sent"
    source = db.scalar(select(ChatMessage).where(ChatMessage.task_id == task_id))
    assert source is None
    assert db.get(OutboundMessage, outbound_id).content == ""
    assert db.scalar(select(Memory.id).where(Memory.content == "今天在窗边读了一篇短诗")) is None


@pytest.mark.asyncio
async def test_ordinary_reply_uses_gateway_even_during_contact_quiet_hours(
    db, engine, settings, monkeypatch
):
    user_id, task_id, outbound_id, _ = seed_contact(db)
    task = db.get(Task, task_id)
    task.kind, task.source = "chat", "qq"
    preferences = db.get(ContactPreference, user_id)
    minute = datetime.now(UTC).hour * 60 + datetime.now(UTC).minute
    preferences.timezone = "UTC"
    preferences.quiet_start = (minute - 1) % 1440
    preferences.quiet_end = (minute + 2) % 1440
    db.commit()
    await send_once(engine, settings.model_copy(update={"proactive_enabled": True}), monkeypatch)
    db.expire_all()
    assert db.get(OutboundMessage, outbound_id).status == "sent"
