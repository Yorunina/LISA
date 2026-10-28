from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from lisa.chat_context import (
    CONTEXT_TTL,
    MAX_CONTEXT_BYTES,
    MAX_TURNS,
    cleanup_context,
    clear_context,
    context_messages,
    remember_delivery,
    remember_turn,
)
from lisa.memory import MemoryBody, delete_memory, edit_memory
from lisa.models import Account, ChatMessage, ChatTurn, Memory, OutboundMessage, QQBinding, Task


def owner(db, *, qq="710000001"):
    user = Account(username="context_" + uuid4().hex[:10], password_hash="synthetic")
    db.add(user)
    db.flush()
    db.add(QQBinding(user_id=user.id, qq_id=qq))
    db.flush()
    return user


def turn(db, user, content, when, *, qq="710000001", status="completed", **message_changes):
    task = Task(user_id=user.id, kind="chat", status=status, created_at=when)
    db.add(task)
    db.flush()
    message = ChatMessage(
        user_id=user.id, task_id=task.id, role="user", content=content,
        sent_at=when, created_at=when, **message_changes,
    )
    db.add(message)
    db.flush()
    remember_turn(db, user, task, qq, [message], now=when)
    return task, message


def delivery(db, task, content, when, *, sequence=0, status="sent", qq="710000001"):
    message = OutboundMessage(
        user_id=task.user_id, task_id=task.id, qq_id=qq, content=content,
        status=status, sequence=sequence, finished_at=when,
    )
    db.add(message)
    db.flush()
    remember_delivery(db, message, now=when)
    return message


def test_context_retains_sources_without_image_data_and_survives_new_session(db, engine):
    from sqlalchemy.orm import Session

    now = datetime.now(UTC)
    user = owner(db)
    task, source = turn(db, user, "看看这盆植物", now,
                        image_urls=["https://private.invalid/signed"],
                        image_description="不应保留的视觉观察")
    delivery(db, task, "叶子挺精神的。", now)
    db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
    user_id, task_id, source_id = user.id, task.id, source.id
    db.commit()
    with Session(engine) as fresh:
        messages = context_messages(fresh, user_id, "710000001", now=now)
        assert [m["content"] for m in messages] == ["看看这盆植物", "叶子挺精神的。"]
        assert messages[0]["task_id"] == str(task_id)
        assert messages[0]["message_id"] == str(source_id)
        assert "private.invalid" not in str(messages) and "视觉观察" not in str(messages)
        assert context_messages(fresh, user_id, "710000001", now=now,
                                exclude_task_id=task_id) == []


def test_round_limit_evicts_whole_oldest_turns_and_parts_share_one_round(db):
    now = datetime.now(UTC)
    user = owner(db)
    for number in range(MAX_TURNS + 1):
        when = now + timedelta(seconds=number)
        task, _ = turn(db, user, f"问题{number}", when)
        for sequence in range(3):
            delivery(db, task, f"回复{number}-{sequence}", when, sequence=sequence)
    messages = context_messages(db, user.id, "710000001", now=now + timedelta(seconds=7))
    assert len(messages) == MAX_TURNS * 4
    assert all(m["content"] not in {"问题0", "回复0-0", "回复0-1", "回复0-2"} for m in messages)
    assert db.scalar(select(func.count()).select_from(ChatTurn)) == MAX_TURNS


def test_context_byte_budget_counts_utf8_and_does_not_truncate_turns(db):
    now = datetime.now(UTC)
    user = owner(db)
    first, _ = turn(db, user, "旧" * 1000, now)
    delivery(db, first, "旧答", now)
    recent, _ = turn(db, user, "新" * 1000, now + timedelta(seconds=1))
    delivery(db, recent, "新答", now + timedelta(seconds=1))
    messages = context_messages(db, user.id, "710000001", now=now + timedelta(seconds=2))
    assert [m["content"] for m in messages] == ["新" * 1000, "新答"]
    assert sum(len(m["content"].encode()) for m in messages) <= MAX_CONTEXT_BYTES


def test_oversize_input_is_omitted_whole_without_discarding_existing_context(db):
    now = datetime.now(UTC)
    user = owner(db)
    turn(db, user, "前一个对象", now)
    turn(db, user, "中" * (MAX_CONTEXT_BYTES // 3 + 1), now + timedelta(seconds=1))
    assert [m["content"] for m in context_messages(
        db, user.id, "710000001", now=now + timedelta(seconds=2)
    )] == ["前一个对象"]


def test_context_expiry_is_not_extended_by_new_turn_or_delivery(db):
    now = datetime.now(UTC)
    user = owner(db)
    original, _ = turn(db, user, "即将过期的对象", now)
    late = now + CONTEXT_TTL - timedelta(seconds=1)
    delivery(db, original, "晚一点送达", late)
    turn(db, user, "新的对象", late)
    messages = context_messages(db, user.id, "710000001", now=now + CONTEXT_TTL)
    assert [m["content"] for m in messages] == ["新的对象"]
    assert db.get(ChatTurn, original.id) is None


@pytest.mark.parametrize("status", ["pending", "sending", "failed", "unknown", "cancelled"])
def test_unconfirmed_parts_never_enter_context(db, status):
    now = datetime.now(UTC)
    user = owner(db)
    task, _ = turn(db, user, "问题", now)
    delivery(db, task, "不应视为已告知", now, status=status)
    assert [m["content"] for m in context_messages(db, user.id, "710000001", now=now)] == ["问题"]


def test_sent_parts_are_immediate_and_idempotent_without_unknown_tail(db):
    now = datetime.now(UTC)
    user = owner(db)
    task, _ = turn(db, user, "分条问题", now)
    first = delivery(db, task, "已送达第一条", now)
    remember_delivery(db, first, now=now)
    delivery(db, task, "未知第二条", now, sequence=1, status="unknown")
    assert [m["content"] for m in context_messages(db, user.id, "710000001", now=now)] == [
        "分条问题", "已送达第一条",
    ]


def test_cleared_context_cannot_be_recreated_by_a_late_delivery(db):
    now = datetime.now(UTC)
    user = owner(db)
    task, _ = turn(db, user, "清除前的请求", now)
    clear_context(db, user.id)
    delivery(db, task, "迟到结果", now + timedelta(seconds=1))
    assert context_messages(db, user.id, "710000001", now=now + timedelta(seconds=1)) == []


def test_other_accounts_and_replaced_bindings_do_not_share_context(db):
    now = datetime.now(UTC)
    user = owner(db)
    other = owner(db, qq="710000002")
    task, _ = turn(db, user, "账号一的对象", now)
    assert context_messages(db, other.id, "710000002", now=now) == []
    db.get(QQBinding, user.id).qq_id = "710000003"
    db.flush()
    assert context_messages(db, user.id, "710000003", now=now) == []
    turn(db, user, "新绑定的对象", now, qq="710000003")
    delivery(db, task, "发给旧绑定的迟到结果", now)
    assert [m["content"] for m in context_messages(db, user.id, "710000003", now=now)] == [
        "新绑定的对象",
    ]


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_memory_mutations_clear_window_and_prevent_delivery_revival(db, operation):
    now = datetime.now(UTC)
    user = owner(db)
    task, _ = turn(db, user, "包含旧记忆的说法", now)
    memory = Memory(user_id=user.id, kind="fact", content="待纠正记录")
    db.add(memory)
    db.commit()
    if operation == "edit":
        edit_memory(memory.id, MemoryBody(content="正确记录"), db, user)
    else:
        delete_memory(memory.id, db, user)
    assert db.scalar(select(func.count()).select_from(ChatTurn)) == 0
    delivery(db, task, "旧请求的迟到回复", now)
    assert context_messages(db, user.id, "710000001", now=now) == []


def test_periodic_cleanup_removes_expired_inactive_and_revision_changed_rows(db):
    now = datetime.now(UTC)
    users = [owner(db, qq=f"71000000{number}") for number in range(1, 5)]
    for user in users:
        turn(db, user, "临时内容", now, qq=db.get(QQBinding, user.id).qq_id)
    users[0].active = False
    users[1].memory_revision += 1
    row = db.scalar(select(ChatTurn).where(ChatTurn.user_id == users[2].id))
    row.expires_at = now
    db.flush()
    cleanup_context(db, now=now)
    assert list(db.scalars(select(ChatTurn.user_id))) == [users[3].id]


def test_account_deletion_cascades_temporary_context(db):
    now = datetime.now(UTC)
    user = owner(db)
    turn(db, user, "删除账号时清除", now)
    db.delete(user)
    db.commit()
    assert db.scalar(select(func.count()).select_from(ChatTurn)) == 0


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
def test_only_successful_chat_turns_are_retained(db, status):
    now = datetime.now(UTC)
    user = owner(db)
    turn(db, user, "未采用输入", now, status=status)
    assert context_messages(db, user.id, "710000001", now=now) == []
