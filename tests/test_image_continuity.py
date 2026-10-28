from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_works import owner as owner

from lisa.chat_context import remember_delivery, remember_turn
from lisa.images import ImageAction, apply_image_action
from lisa.models import Account, ChatMessage, ChatTurn, OutboundMessage, Task


@pytest.fixture
def drawing_settings(settings):
    return settings.model_copy(update={
        "image_model": "synthetic-image",
        "image_base_url": "https://example.invalid/v1",
        "image_api_key": SecretStr("synthetic-image-key"),
    })


def saved_turn(db, owner, text="给我生成一张图", *, minutes=1):
    user = db.get(Account, owner)
    when = datetime.now(UTC) - timedelta(minutes=minutes)
    task = Task(user_id=owner, kind="chat", status="completed", created_at=when)
    db.add(task)
    db.flush()
    message = ChatMessage(
        user_id=owner, task_id=task.id, role="user", content=text,
        sent_at=when, created_at=when,
    )
    db.add(message)
    db.flush()
    remember_turn(db, user, task, "720000001", [message])
    db.delete(message)
    db.flush()
    return task


def current_turn(db, owner):
    task = Task(user_id=owner, kind="chat", status="completed")
    db.add(task)
    db.flush()
    return task


def followup(source, **changes):
    return ImageAction(
        target="chat", requested=True, goal="窗边的一盆绿植",
        evidence="给我生成一张图", source_task_id=source.id,
        current_evidence="窗边的一盆绿植", **changes,
    )


def image_count(db):
    return db.scalar(select(func.count()).select_from(Task).where(Task.kind == "image"))


def test_cross_turn_theme_completes_request_without_drawing_keywords(
    db, owner, drawing_settings
):
    source = saved_turn(db, owner)
    current = current_turn(db, owner)
    notice = apply_image_action(
        db, drawing_settings, db.get(Account, owner), current,
        followup(source), ["窗边的一盆绿植"],
    )
    db.flush()
    image = db.scalar(select(Task).where(Task.kind == "image"))
    assert "安排绘图" in notice and image_count(db) == 1
    assert image.request["source_task_id"] == str(current.id)
    assert image.request["commission_task_id"] == str(source.id)
    assert "evidence" not in image.request and "current_evidence" not in image.request
    assert "给我生成" not in image.input_snapshot["prompt"]


@pytest.mark.parametrize("failure", [
    "empty_evidence", "missing_source", "assistant_quote", "missing_current",
    "old_current_quote", "expired", "changed_memory",
])
def test_invalid_cross_turn_evidence_cannot_fall_back_to_spontaneous_generation(
    db, owner, drawing_settings, failure
):
    source = saved_turn(db, owner, text="说说今天的天气" if failure == "assistant_quote"
                        else "给我生成一张图")
    proposal = followup(source)
    if failure == "empty_evidence":
        proposal.evidence = ""
    elif failure == "missing_source":
        proposal.source_task_id = uuid4()
    elif failure == "assistant_quote":
        message = OutboundMessage(
            user_id=owner, task_id=source.id, qq_id="720000001", sequence=0,
            content="给我生成一张图", status="sent", finished_at=datetime.now(UTC),
        )
        db.add(message)
        db.flush()
        remember_delivery(db, message)
    elif failure == "missing_current":
        proposal.current_evidence = ""
    elif failure == "old_current_quote":
        proposal.current_evidence = "给我生成一张图"
    elif failure == "expired":
        db.get(ChatTurn, source.id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif failure == "changed_memory":
        db.get(Account, owner).memory_revision += 1
    db.flush()
    current = current_turn(db, owner)
    notice = apply_image_action(
        db, drawing_settings, db.get(Account, owner), current,
        proposal, ["窗边的一盆绿植"],
    )
    assert "依据已不可用" in notice
    assert image_count(db) == 0


@pytest.mark.parametrize("state", [
    "pending", "completed", "failed", "cancelled", "outcome_unknown",
])
def test_old_commission_never_regenerates_from_later_confirmation(
    db, owner, drawing_settings, state
):
    source = saved_turn(db, owner)
    first = current_turn(db, owner)
    apply_image_action(db, drawing_settings, db.get(Account, owner), first,
                       followup(source), ["窗边的一盆绿植"])
    db.flush()
    image = db.scalar(select(Task).where(Task.kind == "image"))
    image.status = state
    second = current_turn(db, owner)
    notice = apply_image_action(
        db, drawing_settings, db.get(Account, owner), second,
        followup(source).model_copy(update={"current_evidence": "照一张看看"}),
        ["照一张看看"],
    )
    assert "不会重复生成" in notice and image_count(db) == 1
    if state != "pending":
        third = current_turn(db, owner)
        new = ImageAction(target="chat", requested=True, evidence="再画一张",
                          goal="同一盆绿植的另一幅构图")
        notice = apply_image_action(db, drawing_settings, db.get(Account, owner), third,
                                    new, ["再画一张"])
        db.flush()
        assert "安排绘图" in notice and image_count(db) == 2


def test_explicit_current_request_without_evidence_is_rejected(db, owner, drawing_settings):
    current = current_turn(db, owner)
    proposal = ImageAction(target="chat", requested=True, goal="窗边绿植")
    notice = apply_image_action(db, drawing_settings, db.get(Account, owner), current,
                               proposal, ["看看这盆绿植"])
    assert "依据已不可用" in notice and image_count(db) == 0
