from datetime import UTC, datetime
from uuid import UUID

import pytest
from procrastinate.exceptions import JobRetry
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.chat import execute_chat, ingest_message
from lisa.dialogue import Commission, Dialogue, Opinion, apply_decision
from lisa.model_calls import ModelBusy
from lisa.models import Account, OutboundMessage, Project, Task


def decide(*, opinions=None, commission=None):
    return Dialogue(
        reply="我们先把这个场景确定下来。", opinions=opinions or [], commission=commission
    )


def test_only_current_grounded_opinions_update_their_own_category(db, owner, project):
    work = db.get(Project, UUID(project["id"]))
    work.constraints = "主角不能死亡。"
    work.preferences = "偏好慢节奏。"
    decision = decide(
        opinions=[
            Opinion(
                field="constraints",
                text="允许主角牺牲。",
                replaces="主角不能死亡。",
                evidence="允许牺牲",
            ),
            Opinion(field="preferences", text="可能喜欢悲剧。", replaces="", evidence="允许牺牲"),
            Opinion(
                field="agreement", text="已同意整部故事", replaces="", evidence="历史中别人的意见"
            ),
        ]
    )
    apply_decision(db, db.get(Account, owner), work.id, ["现在允许牺牲"], decision)
    db.flush()
    assert work.constraints == "允许主角牺牲。"
    assert work.preferences == "偏好慢节奏。\n可能喜欢悲剧。"
    assert not work.agreement
    assert db.scalar(select(Task.id)) is None


def test_tentative_discussion_cannot_start_autonomous_writing(db, owner, project):
    work = db.get(Project, UUID(project["id"]))
    work.agreement = "旧的基本共识"
    decision = decide(
        commission=Commission(
            kind="scene",
            goal="写一个场景",
            target_text="",
            evidence="也许可以",
            requested=False,
        )
    )
    notice = apply_decision(db, db.get(Account, owner), work.id, ["也许可以"], decision)
    assert "没有安排" in notice
    assert db.scalar(select(Task.id)) is None


@pytest.mark.asyncio
async def test_explicit_qq_commission_is_atomic_and_keeps_original_project(
    settings,
    engine,
    db,
    owner,
    project,
):
    work_id = UUID(project["id"])
    db.get(Project, work_id).agreement = "车站收到信"
    task_id = ingest_message(
        db,
        bot_id="synthetic",
        message_id="1",
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content="请写收到信的短场景",
    )
    db.commit()

    async def generate(*args):
        assert "车站收到信" in args[-2]
        with Session(engine) as session, session.begin():
            session.get(Project, work_id).is_current = False
            session.add(Project(user_id=owner, title="另一个作品", is_current=True, kind="novel"))
        return decide(
            commission=Commission(
                kind="scene",
                goal="收到信",
                target_text="",
                evidence="请写收到信的短场景",
                requested=True,
            )
        )

    await execute_chat(settings, engine, task_id, generate=generate)
    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    tasks = db.scalars(select(Task).where(Task.kind == "scene")).all()
    assert len(tasks) == 1 and tasks[0].project_id == work_id
    assert tasks[0].source == "qq" and tasks[0].request["notify_qq"]
    assert db.get(Task, task_id).status == "completed"
    notice = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert "已安排" in notice.content


def test_new_agreement_can_start_one_finite_task_without_unsolicited_qq(db, owner, project):
    work = db.get(Project, UUID(project["id"]))
    user = db.get(Account, owner)
    user.last_active_at = datetime.now(UTC)
    decision = decide(
        opinions=[
            Opinion(
                field="agreement",
                text="在雨城收到匿名信",
                replaces="",
                evidence="就按雨城收信这个方向",
            )
        ],
        commission=Commission(
            kind="scene",
            goal="车站收到信",
            target_text="",
            evidence="就按雨城收信这个方向",
            requested=False,
        ),
    )
    first = apply_decision(db, user, work.id, ["就按雨城收信这个方向"], decision)
    second = apply_decision(db, user, work.id, ["就按雨城收信这个方向"], decision)
    task = db.scalar(select(Task).where(Task.kind == "scene"))
    assert "已安排" in first and "没有叠加" in second
    assert task.source == "autonomous" and not task.request["notify_qq"]


def test_decision_cannot_modify_other_users_project(db, owner, project):
    other = Account(username="other_decision_reader", password_hash="synthetic")
    db.add(other)
    db.flush()
    decision = decide(
        opinions=[Opinion(field="constraints", text="越权", replaces="", evidence="越权")]
    )
    apply_decision(db, other, UUID(project["id"]), ["越权"], decision)
    assert db.get(Project, UUID(project["id"])).constraints == ""


def test_unspecified_rewrite_never_guesses_last_paragraph(db, owner, project, chapter):
    decision = decide(
        commission=Commission(
            kind="rewrite",
            goal="重写开头",
            target_text="",
            evidence="重写开头",
            requested=True,
        )
    )
    notice = apply_decision(db, db.get(Account, owner), UUID(project["id"]), ["重写开头"], decision)
    assert "没有定位" in notice
    assert db.scalar(select(Task.id)) is None


@pytest.mark.asyncio
async def test_busy_chat_keeps_original_project_after_switch(settings, engine, db, owner, project):
    work_id = UUID(project["id"])
    task_id = ingest_message(
        db,
        bot_id="synthetic",
        message_id="busy",
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content="不能出现魔法",
    )
    db.commit()

    async def busy(*args):
        raise ModelBusy()

    with pytest.raises(JobRetry):
        await execute_chat(settings, engine, task_id, generate=busy)
    db.expire_all()
    db.get(Project, work_id).is_current = False
    other = Project(user_id=owner, title="其他作品", kind="novel", is_current=True)
    db.add(other)
    db.commit()

    async def generate(*args):
        return decide(
            opinions=[
                Opinion(field="constraints", text="没有魔法", replaces="", evidence="不能出现魔法")
            ]
        )

    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).project_id == work_id
    assert db.get(Project, work_id).constraints == "没有魔法"
    assert db.get(Project, other.id).constraints == ""


@pytest.mark.asyncio
async def test_oversized_project_keeps_plain_chat_without_saving_decisions(
    settings, engine, db, owner, project
):
    work = db.get(Project, UUID(project["id"]))
    work.constraints = "必须保留的规则。" * 2000
    task_id = ingest_message(
        db,
        bot_id="synthetic",
        message_id="large-project",
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content="今天好累，想聊会天。",
    )
    db.commit()

    async def generate(*args):
        assert "只聊天" in args[-2]
        assert "合成作品测试。" in args[-2]
        assert args[-1][-1]["content"].endswith("今天好累，想聊会天。")
        return "今天先休息一会。"

    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(Project, work.id).constraints == "必须保留的规则。" * 2000
    assert db.scalar(select(Task.id).where(Task.kind == "scene")) is None
    notice = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert "网页操作" in notice.content and "今天先休息一会" in notice.content
