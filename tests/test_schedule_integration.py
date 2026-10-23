import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from procrastinate.exceptions import JobRetry
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_chat import ingest
from test_works import HEADERS, RESULT
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.author import execute_author
from lisa.chat import execute_chat
from lisa.model_calls import ModelBusy
from lisa.models import Account, ActivityPlan, Chapter, OutboundMessage, Project, QQBinding, Task
from lisa.scheduling import enqueue_due_plans
from lisa.works import create_writing_task

GOAL = "写雨中发现信件的一个短场景。"


@pytest.fixture
def autonomy_settings(settings):
    return settings.model_copy(
        update={
            "autonomy_enabled": True,
            "autonomous_scene_interval_minutes": 1,
            "autonomous_scene_limit": 2,
            "embedding_model": "",
        }
    )


def response(**changes):
    return {
        "reply": "按已经确定的方向继续。",
        "opinions": [],
        "commission": None,
        "scene_plan": {"goal": GOAL, "delay_minutes": 15},
        **changes,
    }


def receive(db, user_id, event_id, *, sent_at=None, content="按已确定的方向继续写。"):
    return ingest(
        db, event_id, qq_id=db.get(QQBinding, user_id).qq_id, content=content, sent_at=sent_at
    )


async def establish_plan(
    settings, engine, db, user_id, project, *, sent_at=None, event_id="plan-origin"
):
    db.get(Project, UUID(project["id"])).agreement = GOAL
    db.commit()
    task_id = receive(db, user_id, event_id, sent_at=sent_at)
    await execute_chat(settings, engine, task_id, generate=AsyncMock(return_value=response()))
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    plan = db.scalar(
        select(ActivityPlan).where(
            ActivityPlan.user_id == user_id, ActivityPlan.status == "pending"
        )
    )
    assert plan is not None
    return plan, task_id


def make_due(db, settings, plan):
    plan.due_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert enqueue_due_plans(db, settings) == 1
    db.commit()
    db.refresh(plan)
    assert plan.status == "enqueued" and plan.task_id is not None
    return plan.task_id


@pytest.mark.asyncio
async def test_chat_saves_agreement_before_planning_and_creates_no_future_task(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    message = "同意只写雨中发现信件的一个短场景。"
    chat_id = receive(db, owner, "agreement-first", content=message)
    proposal = response(
        opinions=[
            {
                "field": "agreement",
                "text": GOAL,
                "replaces": "",
                "evidence": message,
            }
        ]
    )
    await execute_chat(
        autonomy_settings, engine, chat_id, generate=AsyncMock(return_value=proposal)
    )
    db.expire_all()
    assert GOAL in db.get(Project, UUID(project["id"])).agreement
    plan = db.scalar(select(ActivityPlan).where(ActivityPlan.user_id == owner))
    assert plan is not None and plan.task_id is None and plan.status == "pending"
    assert plan.goal == GOAL and plan.due_at > datetime.now(UTC)
    assert list(db.scalars(select(Task).where(Task.kind == "scene"))) == []
    assert enqueue_due_plans(db, autonomy_settings) == 0


@pytest.mark.asyncio
async def test_explicit_commission_wins_over_duplicate_plan(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    db.get(Project, UUID(project["id"])).agreement = GOAL
    db.commit()
    message = "现在请写雨中发现信件的短场景。"
    chat_id = receive(db, owner, "commission", content=message)
    proposal = response(
        commission={
            "kind": "scene",
            "goal": GOAL,
            "target_text": "",
            "evidence": message,
            "requested": True,
        }
    )
    await execute_chat(
        autonomy_settings, engine, chat_id, generate=AsyncMock(return_value=proposal)
    )
    tasks = list(db.scalars(select(Task).where(Task.kind == "scene")))
    assert len(tasks) == 1 and tasks[0].source == "qq"
    assert tasks[0].request["notify_qq"] is True
    assert list(db.scalars(select(ActivityPlan))) == []


@pytest.mark.parametrize("already_enqueued", [False, True])
@pytest.mark.parametrize("delivery", ["same_timestamp", "delayed"])
@pytest.mark.asyncio
async def test_new_distinct_message_invalidates_old_work_before_understanding(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
    already_enqueued,
    delivery,
):
    sent_at = datetime.now(UTC) - timedelta(minutes=10)
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project, sent_at=sent_at)
    task_id = make_due(db, autonomy_settings, plan) if already_enqueued else None
    assert receive(db, owner, "plan-origin", sent_at=sent_at) is None
    db.refresh(plan)
    assert plan.status == ("enqueued" if already_enqueued else "pending")
    timestamp = sent_at if delivery == "same_timestamp" else sent_at - timedelta(hours=1)
    chat_id = receive(db, owner, "distinct-new-message", sent_at=timestamp)
    db.refresh(plan)
    assert plan.status == "cancelled"
    if task_id:
        db.expire_all()
        assert db.get(Task, task_id).status == "cancelled"
        assert db.get(Task, task_id).started_at is None
    assert enqueue_due_plans(db, autonomy_settings) == 0
    db.commit()
    assert list(db.scalars(select(ActivityPlan).where(ActivityPlan.status == "pending"))) == []
    await execute_chat(
        autonomy_settings, engine, chat_id, generate=AsyncMock(return_value=response())
    )
    db.expire_all()
    new = db.scalar(select(ActivityPlan).where(ActivityPlan.status == "pending"))
    if delivery == "delayed":
        assert new is None  # An older event cannot advance the latest activity window.
    else:
        assert new is not None and new.id != plan.id and new.task_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_late_autonomous_commission_waits_for_pending_chat_but_explicit_order_stays_explicit(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
    requested,
):
    message = "同意雨中发现信件的方向，现在请写一个短场景。"
    first = receive(db, owner, "late-commission-first", content=message)
    received = []

    async def generate(*args):
        with Session(engine) as current:
            received.append(
                receive(
                    current,
                    owner,
                    "late-commission-second",
                    content="还有一个细节稍后说。" if requested else "先别写，我要改方向。",
                )
            )
        return response(
            opinions=[{"field": "agreement", "text": GOAL, "replaces": "", "evidence": message}],
            commission={
                "kind": "scene",
                "goal": GOAL,
                "target_text": "",
                "evidence": message,
                "requested": requested,
            },
        )

    await execute_chat(autonomy_settings, engine, first, generate=generate)
    db.expire_all()
    assert db.get(Task, received[0]).status == "pending"
    tasks = list(db.scalars(select(Task).where(Task.kind == "scene")))
    if requested:
        assert len(tasks) == 1 and tasks[0].source == "qq"
        assert tasks[0].request["notify_qq"] is True
    else:
        assert tasks == []
    assert list(db.scalars(select(ActivityPlan).where(ActivityPlan.status == "pending"))) == []


@pytest.mark.asyncio
async def test_new_message_during_chat_must_be_understood_before_plan_is_saved(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    db.get(Project, UUID(project["id"])).agreement = GOAL
    db.commit()
    sent_at = datetime.now(UTC) - timedelta(minutes=1)
    first = receive(db, owner, "understanding-first", sent_at=sent_at)
    received = []

    async def generate(*args):
        with Session(engine) as current:
            received.append(receive(current, owner, "understanding-second", sent_at=sent_at))
        return response()

    await execute_chat(autonomy_settings, engine, first, generate=generate)
    db.expire_all()
    assert received[0] != first
    assert db.get(Task, received[0]).status == "pending"
    assert list(db.scalars(select(ActivityPlan).where(ActivityPlan.status == "pending"))) == []
    assert enqueue_due_plans(db, autonomy_settings) == 0
    db.commit()
    await execute_chat(
        autonomy_settings, engine, received[0], generate=AsyncMock(return_value=response())
    )
    db.expire_all()
    assert len(list(db.scalars(select(ActivityPlan).where(ActivityPlan.status == "pending")))) == 1


@pytest.mark.asyncio
async def test_new_qq_after_pause_replans_without_running_old_plan(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    stale = datetime.now(UTC) - timedelta(days=2)
    db.get(Account, owner).last_active_at = stale
    plan.activity_at = stale
    plan.due_at = stale + timedelta(minutes=15)
    db.commit()
    assert enqueue_due_plans(db, autonomy_settings) == 0
    db.commit()
    new_chat = receive(db, owner, "resume-new-qq", content="今天回来继续讨论。")
    assert enqueue_due_plans(db, autonomy_settings) == 0
    db.commit()
    assert list(db.scalars(select(Task).where(Task.kind == "scene"))) == []
    await execute_chat(
        autonomy_settings, engine, new_chat, generate=AsyncMock(return_value=response())
    )
    db.expire_all()
    new_plan = db.scalar(select(ActivityPlan).where(ActivityPlan.status == "pending"))
    assert new_plan.id != plan.id
    assert new_plan.activity_at > stale and new_plan.task_id is None
    assert db.get(ActivityPlan, plan.id).status == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_planned_task_is_never_automatically_requeued(
    autonomy_settings,
    engine,
    client,
    db,
    owner,
    project,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    task_id = make_due(db, autonomy_settings, plan)
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
    assert enqueue_due_plans(db, autonomy_settings) == 0
    db.commit()
    generate = AsyncMock(side_effect=AssertionError("Cancelled scheduled work must not execute"))
    await execute_author(autonomy_settings, engine, task_id, generate=generate)
    generate.assert_not_awaited()
    assert [task.id for task in db.scalars(select(Task).where(Task.kind == "scene"))] == [task_id]


@pytest.mark.parametrize("kind", ["scene", "rewrite"])
@pytest.mark.asyncio
async def test_future_plan_does_not_block_explicit_web_work(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
    chapter,
    kind,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    task = create_writing_task(
        db,
        db.get(Account, owner),
        db.get(Project, UUID(project["id"])),
        kind,
        "只完成这次明确的网页创作。",
        chapter_id=chapter if kind == "rewrite" else None,
        target_text="旧的结尾。" if kind == "rewrite" else None,
    )
    task_id = task.id
    db.commit()
    await execute_author(
        autonomy_settings, engine, task_id, generate=AsyncMock(return_value=RESULT)
    )
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(ActivityPlan, plan.id).status == "pending"
    assert db.get(ActivityPlan, plan.id).task_id is None


@pytest.mark.parametrize("change", ["new_message", "expired"])
@pytest.mark.asyncio
async def test_busy_unstarted_plan_rechecks_new_message_and_activity_expiry(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
    change,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    task_id = make_due(db, autonomy_settings, plan)
    generate = AsyncMock(side_effect=ModelBusy())
    with pytest.raises(JobRetry):
        await execute_author(autonomy_settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.started_at is None and task.input_snapshot is None
    if change == "new_message":
        receive(db, owner, "busy-new-qq")
    else:
        db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(days=2)
        db.commit()
    await execute_author(autonomy_settings, engine, task_id, generate=generate)
    assert generate.await_count == 1
    db.expire_all()
    assert db.get(Task, task_id).status in {"failed", "cancelled"}
    assert db.get(Task, task_id).adopted_at is None


@pytest.mark.asyncio
async def test_planned_author_reads_latest_materials_after_waiting_for_slot(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    work = db.get(Project, UUID(project["id"]))
    work.constraints = "规划时的旧约束。"
    db.commit()
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    task_id = make_due(db, autonomy_settings, plan)
    work.constraints = "真正开始前保存的新约束。"
    db.commit()
    snapshots = []

    async def generate(*args):
        snapshots.append(json.loads(args[-1][0]["content"]))
        if len(snapshots) == 1:
            raise ModelBusy()
        return RESULT

    with pytest.raises(JobRetry):
        await execute_author(autonomy_settings, engine, task_id, generate=generate)
    db.expire_all()
    db.get(Project, work.id).constraints = "等待槽位期间再次保存的约束。"
    db.commit()
    await execute_author(autonomy_settings, engine, task_id, generate=generate)
    assert snapshots[0]["constraints"] == "真正开始前保存的新约束。"
    assert snapshots[1]["constraints"] == "等待槽位期间再次保存的约束。"
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_started_planned_author_finishes_after_24h_without_proactive_qq(
    autonomy_settings,
    engine,
    db,
    owner,
    project,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    task_id = make_due(db, autonomy_settings, plan)

    async def generate(*args):
        with Session(engine) as current, current.begin():
            assert current.get(Task, task_id).started_at is not None
            current.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(days=2)
        return RESULT

    await execute_author(autonomy_settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed" and task.adopted_at is not None
    assert db.get(Chapter, task.chapter_id).title == RESULT["title"]
    assert list(db.scalars(select(OutboundMessage).where(OutboundMessage.task_id == task_id))) == []


@pytest.mark.asyncio
async def test_schedule_endpoint_requires_login_isolates_owner_and_reports_pause(
    autonomy_settings,
    engine,
    client,
    db,
    owner,
    project,
    monkeypatch,
):
    plan, _ = await establish_plan(autonomy_settings, engine, db, owner, project)
    other = Account(username="other_schedule_reader", password_hash="synthetic-unused")
    db.add(other)
    db.flush()
    other_project = Project(user_id=other.id, title="另一账号作品")
    db.add(other_project)
    db.flush()
    db.add(
        ActivityPlan(
            user_id=other.id,
            project_id=other_project.id,
            goal="另一账号的私有计划",
            due_at=datetime.now(UTC),
            activity_at=datetime.now(UTC),
        )
    )
    db.commit()
    monkeypatch.setattr(client.app.state, "settings", autonomy_settings)
    result = client.get("/api/schedule")
    assert result.status_code == 200
    assert result.json()["status"] == "scheduled"
    assert result.json()["goal"] == plan.goal
    assert result.json()["project_id"] == project["id"]
    assert "另一账号" not in result.text
    db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(days=2)
    db.commit()
    paused = client.get("/api/schedule").json()
    assert paused["status"] == "paused" and paused["pause_reason"] == "inactive"
    assert paused["goal"] is None
    client.cookies.clear()
    assert client.get("/api/schedule").status_code == 401


@pytest.mark.asyncio
async def test_disabled_autonomy_ignores_model_scene_plan(settings, engine, db, owner, project):
    db.get(Project, UUID(project["id"])).agreement = GOAL
    db.commit()
    chat_id = receive(db, owner, "disabled-plan")
    await execute_chat(
        settings.model_copy(update={"autonomy_enabled": False}),
        engine,
        chat_id,
        generate=AsyncMock(return_value=response()),
    )
    db.expire_all()
    assert db.get(Task, chat_id).status == "completed"
    assert list(db.scalars(select(ActivityPlan))) == []
    assert list(db.scalars(select(Task).where(Task.kind == "scene"))) == []
