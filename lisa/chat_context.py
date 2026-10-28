"""Small, expiring context for one account and its current QQ binding."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select

from lisa.models import Account, ChatTurn, QQBinding, Task

MAX_TURNS = 6
MAX_CONTEXT_BYTES = 6000
CONTEXT_TTL = timedelta(minutes=30)


def clear_context(db, user_id):
    db.execute(delete(ChatTurn).where(ChatTurn.user_id == user_id))


def cleanup_context(db, *, now=None):
    """The existing periodic cleanup can remove expired and invalid windows."""
    now = now or datetime.now(UTC)
    current = exists(select(Account.id).join(
        QQBinding, QQBinding.user_id == Account.id
    ).where(
        Account.id == ChatTurn.user_id,
        Account.active.is_(True),
        Account.memory_revision == ChatTurn.memory_revision,
        QQBinding.qq_id == ChatTurn.qq_id,
    ))
    db.execute(delete(ChatTurn).where((ChatTurn.expires_at <= now) | ~current))


def _valid_owner(db, user_id, qq_id):
    return db.scalar(select(Account).join(
        QQBinding, QQBinding.user_id == Account.id
    ).where(
        Account.id == user_id, Account.active.is_(True), QQBinding.qq_id == qq_id,
    ))


def _bounded_turns(db, user_id, qq_id, *, now):
    """Caller holds the account lock; only whole oldest turns are evicted."""
    user = _valid_owner(db, user_id, qq_id)
    if user is None:
        # A late delivery to an old binding must not erase a newer binding's window.
        db.execute(delete(ChatTurn).where(
            ChatTurn.user_id == user_id, ChatTurn.qq_id == qq_id,
        ))
        return []
    db.execute(delete(ChatTurn).where(
        ChatTurn.user_id == user_id,
        (ChatTurn.qq_id != qq_id)
        | (ChatTurn.memory_revision != user.memory_revision)
        | (ChatTurn.expires_at <= now),
    ))
    rows = list(db.scalars(select(ChatTurn).where(
        ChatTurn.user_id == user_id,
    ).order_by(ChatTurn.created_at.desc(), ChatTurn.task_id.desc())))
    kept, total = [], 0
    for row in rows:
        size = sum(len(m["content"].encode("utf-8")) for m in row.messages)
        if len(kept) >= MAX_TURNS or total + size > MAX_CONTEXT_BYTES:
            # Stop at the first overflow rather than retaining a disjoint older fragment.
            for old in rows[len(kept):]:
                db.delete(old)
            break
        kept.append(row)
        total += size
    db.flush()
    return list(reversed(kept))


def context_messages(db, user_id, qq_id, *, now=None, exclude_task_id=None):
    """Return nearby user inputs and confirmed deliveries, with minimal source identity."""
    now = now or datetime.now(UTC)
    return [dict(message) for turn in _bounded_turns(db, user_id, qq_id, now=now)
            if turn.task_id != exclude_task_id for message in turn.messages]


def remember_turn(db, user, task, qq_id, batch, *, now=None):
    """After successful adoption, keep text input without image URLs or visual observations."""
    now = now or datetime.now(UTC)
    owner = _valid_owner(db, user.id, qq_id)
    if owner is None or task.user_id != user.id or task.kind != "chat":
        return
    if task.status != "completed" or db.get(ChatTurn, task.id) is not None:
        return
    inputs = [message for message in batch if message.role == "user"
              and message.user_id == user.id and message.task_id == task.id
              and not message.deleted_at and not message.context_excluded]
    if not inputs:
        return
    received = min(message.created_at for message in inputs)
    expires = received + CONTEXT_TTL
    if expires <= now:
        return
    messages = [{
        "role": "user", "content": message.content,
        "task_id": str(task.id), "message_id": str(message.id),
        "time": message.created_at.isoformat(),
    } for message in inputs]
    if sum(len(message["content"].encode("utf-8")) for message in messages) > MAX_CONTEXT_BYTES:
        return
    db.add(ChatTurn(
        task_id=task.id, user_id=user.id, qq_id=qq_id,
        memory_revision=owner.memory_revision, created_at=received,
        expires_at=expires, messages=messages,
    ))
    db.flush()
    _bounded_turns(db, user.id, qq_id, now=now)


def remember_delivery(db, message, *, now=None):
    """Append only confirmed text while its original turn still exists and is valid."""
    if message.status != "sent" or not message.content:
        return
    now = now or datetime.now(UTC)
    task = db.get(Task, message.task_id)
    if task is None or task.kind != "chat" or task.status != "completed":
        return
    rows = _bounded_turns(db, message.user_id, message.qq_id, now=now)
    turn = next((row for row in rows if row.task_id == message.task_id), None)
    if turn is None:
        return
    if any(m.get("role") == "assistant" and m.get("sequence") == message.sequence
           for m in turn.messages):
        return
    sent_at = message.finished_at or now
    if sent_at + CONTEXT_TTL <= now:
        return
    turn.messages = [*turn.messages, {
        "role": "assistant", "content": message.content,
        "task_id": str(task.id), "message_id": str(message.id),
        "time": sent_at.isoformat(), "sequence": message.sequence,
    }]
    # Earliest expiry is never extended by a later part or by later conversation.
    turn.expires_at = min(turn.expires_at, sent_at + CONTEXT_TTL)
    db.flush()
    _bounded_turns(db, message.user_id, message.qq_id, now=now)
