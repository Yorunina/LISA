from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_chat import QQ, create_user
from test_proactive import contact_settings as contact_settings
from test_proactive import contact_user as contact_user
from test_proactive import queued
from test_proactive_delivery import send_once
from test_works import RESULT, document

from lisa.chat import claim_outbound
from lisa.models import Chapter, ChatMessage, Memory, OutboundMessage, Project, Task
from lisa.proactive import execute_contact
from lisa.progress import progress_context, progress_is_current
from lisa.works import adopt_result, save_chapter


def saved_result(db, user_id, *, project=None):
    if project is None:
        project = Project(user_id=user_id, title="雨中的信", agreement="发现信件的一个短场景。")
        db.add(project)
        db.flush()
    task = Task(
        user_id=user_id,
        project_id=project.id,
        kind="scene",
        source="autonomous",
        status="running",
        request={"goal": "发现信件"},
    )
    db.add(task)
    db.flush()
    chapter = adopt_result(db, task, RESULT)
    db.commit()
    return project, task, chapter


@pytest.fixture
def result(db):
    return saved_result(db, create_user(db))


def prior_contact(db, user_id, reference, *, status="sent", hours=1):
    task = Task(
        user_id=user_id,
        kind="contact",
        source="autonomous",
        status="completed",
        input_snapshot={"progress": reference, "life_events": []},
    )
    db.add(task)
    db.flush()
    message = OutboundMessage(
        user_id=user_id,
        task_id=task.id,
        qq_id=QQ,
        content="合成交流",
        status=status,
        finished_at=datetime.now(UTC) - timedelta(hours=hours),
    )
    db.add(message)
    db.flush()
    return message


def test_saved_result_is_optional_context_not_a_new_contact(db, result):
    project, task, chapter = result
    reference, context = progress_context(db, task.user_id)
    assert reference == {
        "task_id": str(task.id),
        "project_id": str(project.id),
        "chapter_id": str(chapter.id),
        "version": chapter.version,
    }
    assert RESULT["paragraphs"][0] in context and project.title in context
    assert "不是进度播报" in context and "不是LISA亲历" in context
    assert progress_is_current(db, task.user_id, reference)
    assert not progress_is_current(db, uuid4(), reference)
    assert not list(db.scalars(select(OutboundMessage)))
    assert not list(db.scalars(select(Task).where(Task.kind == "contact")))


@pytest.mark.parametrize(
    "change",
    [
        "web",
        "qq",
        "failed",
        "cancelled",
        "unadopted",
        "old",
        "future",
        "not_current",
        "edited",
        "other_user",
    ],
)
def test_only_current_recent_autonomous_adopted_results_are_candidates(db, result, change):
    project, task, chapter = result
    user_id = task.user_id
    if change in {"web", "qq"}:
        task.source = change
    elif change in {"failed", "cancelled"}:
        task.status = change
    elif change == "unadopted":
        task.adopted_at = None
    elif change == "old":
        task.adopted_at = datetime.now(UTC) - timedelta(hours=25)
    elif change == "future":
        task.adopted_at = datetime.now(UTC) + timedelta(hours=1)
    elif change == "not_current":
        project.is_current = False
    elif change == "edited":
        save_chapter(db, chapter, title=chapter.title, content=document("用户已经改写了这一段。"))
    else:
        user_id = uuid4()
    db.flush()
    assert progress_context(db, user_id) == (None, "")


@pytest.mark.parametrize(
    "status,blocked",
    [
        ("pending", True),
        ("sending", True),
        ("sent", True),
        ("unknown", True),
        ("failed", False),
        ("cancelled", False),
    ],
)
def test_existing_delivery_controls_duplicate_candidates_without_assuming_unknown_sent(
    db,
    result,
    status,
    blocked,
):
    _, task, _ = result
    reference, _ = progress_context(db, task.user_id)
    prior_contact(db, task.user_id, reference, status=status, hours=2)
    prior_contact(db, task.user_id, None, hours=1)
    candidate, _ = progress_context(db, task.user_id)
    assert (candidate is None) == blocked


def test_progress_is_not_the_center_of_consecutive_contacts(db, result):
    project, task, _ = result
    reference, _ = progress_context(db, task.user_id)
    prior_contact(db, task.user_id, reference, hours=2)
    _, latest, _ = saved_result(db, task.user_id, project=project)
    assert progress_context(db, task.user_id) == (None, "")
    prior_contact(db, task.user_id, None, hours=1)
    candidate, _ = progress_context(db, task.user_id)
    assert candidate["task_id"] == str(latest.id)


def response(task_id):
    evidence = "《雨中的信》也存好了一小段，信还没有打开。"
    return {
        "reply": "今天在窗边听了一会儿雨声。" + evidence,
        "progress_mention": {"task_id": str(task_id), "evidence": evidence},
    }


@pytest.mark.asyncio
async def test_generation_and_confirmed_delivery_preserve_progress_reference(
    db,
    engine,
    contact_settings,
    contact_user,
    monkeypatch,
):
    _, author, _ = saved_result(db, contact_user)
    contact = queued(db, contact_settings)
    generate = AsyncMock(return_value=response(author.id))
    await execute_contact(contact_settings, engine, contact.id, generate=generate)
    db.expire_all()
    assert str(author.id) in generate.call_args.args[4]
    reference = db.get(Task, contact.id).input_snapshot["progress"]
    outbound = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == contact.id))
    assert f"?chapter={reference['chapter_id']}" in outbound.content
    assert "/read/" in outbound.content
    assert reference["task_id"] == str(author.id)
    assert set(reference) == {"task_id", "project_id", "chapter_id", "version"}
    assert not db.scalar(select(ChatMessage).where(ChatMessage.task_id == contact.id))
    assert not list(db.scalars(select(Memory)))
    await send_once(engine, contact_settings, monkeypatch)
    db.expire_all()
    assert (
        db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == contact.id)).status
        == "sent"
    )
    assert db.scalar(select(ChatMessage).where(ChatMessage.task_id == contact.id))
    assert progress_context(db, contact_user) == (None, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["during_generation", "before_send", "during_send"])
async def test_editing_result_prevents_stale_progress_or_excludes_it_after_inflight_send(
    db,
    engine,
    contact_settings,
    contact_user,
    monkeypatch,
    stage,
):
    _, author, chapter = saved_result(db, contact_user)
    chapter_id = chapter.id
    contact = queued(db, contact_settings)
    reply = response(author.id)

    def edit():
        with Session(engine) as session, session.begin():
            current = session.get(Chapter, chapter_id)
            save_chapter(session, current, title=current.title, content=document("新的用户正文。"))

    async def generate(*args):
        if stage == "during_generation":
            edit()
        return reply

    await execute_contact(contact_settings, engine, contact.id, generate=generate)
    db.expire_all()
    if stage == "during_generation":
        assert db.get(Task, contact.id).error == "progress_changed"
        assert not db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == contact.id))
    elif stage == "before_send":
        edit()
        assert claim_outbound(db, QQ, settings=contact_settings) is None
        assert db.get(Task, contact.id).error == "progress_changed"
        assert (
            db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == contact.id)).status
            == "cancelled"
        )
    else:
        await send_once(engine, contact_settings, monkeypatch, before_result=edit)
        db.expire_all()
        source = db.scalar(select(ChatMessage).where(ChatMessage.task_id == contact.id))
        assert source.context_excluded


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong_task", "false_evidence", "omitted_context"])
async def test_progress_claim_must_match_actually_provided_result_and_reply(
    db,
    engine,
    contact_settings,
    contact_user,
    monkeypatch,
    invalid,
):
    _, author, _ = saved_result(db, contact_user)
    contact = queued(db, contact_settings)
    reply = response(author.id)
    if invalid == "wrong_task":
        reply["progress_mention"]["task_id"] = str(uuid4())
    elif invalid == "false_evidence":
        reply["progress_mention"]["evidence"] = "没有出现在回复里的引用。"
    else:
        monkeypatch.setattr("lisa.proactive.interaction_context", lambda *args: "过长观察" * 3000)
    await execute_contact(
        contact_settings, engine, contact.id, generate=AsyncMock(return_value=reply)
    )
    db.expire_all()
    assert db.get(Task, contact.id).status == "failed"
    assert not db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == contact.id))
