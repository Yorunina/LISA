from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_chat import create_user, ingest
from test_proactive import contact_settings as contact_settings
from test_proactive import contact_user as contact_user
from test_proactive import queued

from lisa.chat import execute_chat
from lisa.dialogue import Dialogue, apply_decision, creative_context
from lisa.models import Account, OutboundMessage, Project, Task
from lisa.proactive import execute_contact
from lisa.topics import DEFAULT_MARKER, DefaultDesign, adopt_default_design, topic_context

DISCUSSION = "关于信封要用蓝色还是白色，还没有决定。"
PROPOSAL = "暂时设想白色信封，等用户重提时再调整。"


@pytest.fixture
def topic_project(db):
    user_id = create_user(db)
    project = Project(
        user_id=user_id,
        title="雨中的信",
        tentative=DISCUSSION,
        agreement="只写发现信件的短场景。",
        constraints="现实题材。",
    )
    db.add(project)
    db.commit()
    return project


def design(**changes):
    return DefaultDesign(
        evidence=changes.get("evidence", DISCUSSION), proposal=changes.get("proposal", PROPOSAL)
    )


def test_default_only_appends_labeled_tentative_and_does_not_authorize_writing(db, topic_project):
    old_agreement = topic_project.agreement
    old_revision = topic_project.index_revision
    assert adopt_default_design(
        db, topic_project.user_id, topic_project.id, design(), provided_tentative=DISCUSSION
    )
    assert topic_project.tentative.startswith(DISCUSSION)
    assert DEFAULT_MARKER in topic_project.tentative and PROPOSAL in topic_project.tentative
    assert topic_project.agreement == old_agreement
    assert topic_project.constraints == "现实题材。"
    assert topic_project.index_revision == old_revision + 1
    assert list(db.scalars(select(Task))) == []
    assert not adopt_default_design(
        db, topic_project.user_id, topic_project.id, design(), provided_tentative=DISCUSSION
    )


@pytest.mark.parametrize("case", ["owner", "not_current", "unprovided", "removed", "own_design"])
def test_default_requires_current_owned_and_actually_supplied_unresolved_evidence(
    db,
    topic_project,
    case,
):
    user_id = topic_project.user_id
    provided = DISCUSSION
    candidate = design()
    if case == "owner":
        user_id = uuid4()
    elif case == "not_current":
        topic_project.is_current = False
    elif case == "unprovided":
        provided = ""
    elif case == "removed":
        topic_project.tentative = "已在网页改成其他讨论。"
    else:
        topic_project.tentative = DEFAULT_MARKER + PROPOSAL
        provided = topic_project.tentative
        candidate = design(evidence=PROPOSAL)
    before = topic_project.tentative
    assert not adopt_default_design(
        db, user_id, topic_project.id, candidate, provided_tentative=provided
    )
    assert topic_project.tentative == before


def test_default_preserves_other_web_edits_and_same_evidence_is_not_replanned(db, topic_project):
    topic_project.tentative += "\n网页刚加入另一个关于邮戳的想法。"
    assert adopt_default_design(
        db, topic_project.user_id, topic_project.id, design(), provided_tentative=DISCUSSION
    )
    assert "网页刚加入" in topic_project.tentative
    assert not adopt_default_design(
        db,
        topic_project.user_id,
        topic_project.id,
        design(proposal="改成另一种暂定方案。"),
        provided_tentative=DISCUSSION,
    )


def test_topic_context_keeps_constraints_and_omits_oversized_notes(topic_project):
    context = topic_context(topic_project)
    assert DISCUSSION in context and topic_project.constraints in context
    assert "不是用户确认" in context
    topic_project.tentative = "长" * 2001
    assert topic_context(topic_project) == ""


def test_user_can_revisit_and_replace_old_default_without_creating_agreement(db, topic_project):
    adopt_default_design(
        db, topic_project.user_id, topic_project.id, design(), provided_tentative=DISCUSSION
    )
    line = topic_project.tentative.splitlines()[-1]
    instruction = "先把信封的暂定方案改成蓝色，仍然只是备选，不开始写。"
    decision = Dialogue(
        reply="好，暂时记下。",
        opinions=[
            {
                "field": "tentative",
                "text": "信封暂定蓝色，仍待确认。",
                "replaces": line,
                "evidence": instruction,
            }
        ],
        commission=None,
    )
    original_agreement = topic_project.agreement
    apply_decision(
        db, db.get(Account, topic_project.user_id), topic_project.id, [instruction], decision
    )
    assert "信封暂定蓝色" in topic_project.tentative and PROPOSAL not in topic_project.tentative
    assert topic_project.agreement == original_agreement
    assert not list(db.scalars(select(Task)))
    assert "信封暂定蓝色" in creative_context(topic_project)


@pytest.mark.asyncio
async def test_proactive_default_is_adopted_without_new_writer_and_later_chat_can_revisit(
    db,
    engine,
    contact_settings,
    contact_user,
):
    project = Project(user_id=contact_user, title="雨中的信", tentative=DISCUSSION)
    db.add(project)
    db.commit()
    project_id = project.id
    task = queued(db, contact_settings)
    generate = AsyncMock(
        return_value={"reply": "今天窗边的风很轻。", "default_design": design().model_dump()}
    )
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    db.expire_all()
    assert DISCUSSION in generate.call_args.args[4]
    assert PROPOSAL in db.get(Project, project_id).tentative
    assert not db.get(Project, project_id).agreement
    assert not list(db.scalars(select(Task).where(Task.kind.in_(["scene", "rewrite"]))))
    assert db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task.id))
    chat_id = ingest(db, "revisit-default", content="之前信封那件事，我们再聊聊。")
    chat = AsyncMock(
        return_value={"reply": "好，我们继续讨论。", "opinions": [], "commission": None}
    )
    await execute_chat(contact_settings, engine, chat_id, generate=chat)
    assert PROPOSAL in chat.call_args.args[4]
    assert "不是用户确认" in chat.call_args.args[4]


@pytest.mark.asyncio
async def test_expired_contact_cannot_adopt_default_design(
    db, engine, contact_settings, contact_user
):
    project = Project(user_id=contact_user, title="雨中的信", tentative=DISCUSSION)
    db.add(project)
    db.commit()
    project_id = project.id
    task = queued(db, contact_settings)

    async def generate(*args):
        ingest(db, "new-feedback-during-default", content="等等，我想换个方向。")
        return {"reply": "今天的窗边很安静。", "default_design": design().model_dump()}

    await execute_contact(contact_settings, engine, task.id, generate=generate)
    db.expire_all()
    assert db.get(Project, project_id).tentative == DISCUSSION
    assert db.get(Task, task.id).status == "cancelled"


@pytest.mark.asyncio
async def test_omitted_optional_topic_cannot_produce_a_saved_default(
    db,
    engine,
    contact_settings,
    contact_user,
    monkeypatch,
):
    project = Project(user_id=contact_user, title="雨中的信", tentative=DISCUSSION)
    db.add(project)
    db.commit()
    project_id = project.id
    task = queued(db, contact_settings)
    monkeypatch.setattr("lisa.proactive.topic_context", lambda _: DISCUSSION * 3000)
    generate = AsyncMock(
        return_value={"reply": "窗边的风很轻。", "default_design": design().model_dump()}
    )
    await execute_contact(contact_settings, engine, task.id, generate=generate)
    db.expire_all()
    assert db.get(Task, task.id).status == "completed"
    assert DISCUSSION not in generate.call_args.args[4]
    assert db.get(Project, project_id).tentative == DISCUSSION


@pytest.mark.asyncio
async def test_optional_memories_do_not_crowd_out_current_chat_input(
    db,
    engine,
    settings,
    monkeypatch,
):
    create_user(db)
    task_id = ingest(db, "observation-budget", content="你好，今天聊点轻松的。")
    settings = settings.model_copy(update={"model_max_input_bytes": 11_000})
    monkeypatch.setattr(
        "lisa.chat.memory_context", lambda *args, **kwargs: "过长的可选记忆" * 3000
    )
    generate = AsyncMock(return_value="好，我们轻松聊聊。")
    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert "过长的可选记忆" not in generate.call_args.args[4]
    assert "你好，今天聊点轻松的。" in generate.call_args.args[5][-1]["content"]
