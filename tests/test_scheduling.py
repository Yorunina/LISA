from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lisa.models import Account, ActivityPlan, ChatMessage, Persona, Project, QQBinding, Task
from lisa.scheduling import (
    ScenePlanProposal,
    autonomous_start_allowed,
    discard_pending_plans,
    enqueue_due_plans,
    plan_after_chat,
    schedule_view,
)


@pytest.fixture
def autonomous_settings(settings):
    return settings.model_copy(update={"autonomy_enabled": True})


@pytest.fixture
def scene_context(db):
    now = datetime.now(UTC)
    user = Account(username="scene_planner", password_hash="synthetic", last_active_at=now)
    db.add(user)
    db.flush()
    db.add_all(
        [
            QQBinding(user_id=user.id, qq_id="710005001"),
            Persona(user_id=user.id, name="LISA", prompt="合成测试"),
        ]
    )
    project = Project(user_id=user.id, title="合成作品", agreement="写雨中收到信的短场景")
    chat = Task(user_id=user.id, kind="chat", source="qq", status="completed", created_at=now)
    db.add_all([project, chat])
    db.flush()
    chat.project_id = project.id
    db.add(
        ChatMessage(
            user_id=user.id,
            task_id=chat.id,
            role="user",
            content="按约定继续吧",
            sent_at=now,
        )
    )
    db.commit()
    return user, project, chat, now


def proposal():
    return ScenePlanProposal(goal="写收到信后的反应", delay_minutes=10)


def make_due(db, settings, context):
    _, _, chat, now = context
    plan = plan_after_chat(db, settings, chat, proposal(), now=now)
    plan.due_at = now - timedelta(seconds=1)
    db.flush()
    return plan


def test_future_plan_uses_minimum_interval_without_author_task(
    db, autonomous_settings, scene_context
):
    user, project, chat, now = scene_context
    plan = plan_after_chat(db, autonomous_settings, chat, proposal(), now=now)
    assert plan.due_at == now + timedelta(minutes=120)
    assert plan.activity_at == user.last_active_at
    assert plan.project_id == project.id and plan.task_id is None
    assert enqueue_due_plans(db, autonomous_settings, now=now + timedelta(minutes=119)) == 0
    assert db.scalar(select(func.count()).select_from(Task)) == 1
    assert schedule_view(db, autonomous_settings, user, now).status == "scheduled"


def test_due_plan_enqueues_once_without_qq_notice(db, autonomous_settings, scene_context):
    plan = make_due(db, autonomous_settings, scene_context)
    assert enqueue_due_plans(db, autonomous_settings) == 1
    assert enqueue_due_plans(db, autonomous_settings) == 0
    task = db.get(Task, plan.task_id)
    assert task.queue_job_id is not None
    assert task.kind == "scene" and task.source == "autonomous"
    assert task.request["plan_id"] == str(plan.id)
    assert task.request["activity_at"] == plan.activity_at.isoformat()
    assert task.request["notify_qq"] is False
    assert plan.status == "enqueued"


def test_concurrent_sweeps_create_one_task(db, engine, autonomous_settings, scene_context):
    plan = make_due(db, autonomous_settings, scene_context)
    plan_id = plan.id
    db.commit()
    barrier = Barrier(2)

    def sweep():
        with Session(engine) as other, other.begin():
            barrier.wait(timeout=5)
            return enqueue_due_plans(other, autonomous_settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sweep(), range(2)))
    assert sum(results) == 1
    db.expire_all()
    assert db.get(ActivityPlan, plan_id).task_id is not None
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == "scene")) == 1


@pytest.mark.parametrize("kind,status", [("scene", "pending"), ("rewrite", "running")])
def test_due_plan_waits_for_any_user_writing(db, autonomous_settings, scene_context, kind, status):
    user, _, _, now = scene_context
    plan = make_due(db, autonomous_settings, scene_context)
    other = Project(user_id=user.id, title="另一作品", is_current=False)
    db.add(other)
    db.flush()
    task = Task(user_id=user.id, project_id=other.id, kind=kind, source="web", status=status)
    db.add(task)
    db.flush()
    assert enqueue_due_plans(db, autonomous_settings) == 0
    assert plan.status == "pending" and plan.task_id is None
    assert schedule_view(db, autonomous_settings, user, now).status == "waiting"
    task.status = "completed"
    db.flush()
    assert enqueue_due_plans(db, autonomous_settings) == 1


@pytest.mark.parametrize("change", ["expired", "completed", "switched", "agreement", "new_chat"])
def test_due_plan_cancels_when_no_longer_eligible(db, autonomous_settings, scene_context, change):
    user, project, _, now = scene_context
    plan = make_due(db, autonomous_settings, scene_context)
    if change == "expired":
        now = user.last_active_at + timedelta(hours=24)
    elif change == "completed":
        project.completed = True
    elif change == "switched":
        project.is_current = False
    elif change == "agreement":
        project.agreement = ""
    else:
        db.add(Task(user_id=user.id, kind="chat", source="qq", status="pending"))
    db.flush()
    assert enqueue_due_plans(db, autonomous_settings, now=now) == 0
    assert plan.status == "cancelled"
    assert enqueue_due_plans(db, autonomous_settings, now=now) == 0


def test_old_chat_and_unprocessed_chat_cannot_plan(db, autonomous_settings, scene_context):
    user, _, chat, now = scene_context
    user.last_active_at = now + timedelta(seconds=1)
    db.flush()
    assert plan_after_chat(db, autonomous_settings, chat, proposal(), now=now) is None
    user.last_active_at = now
    later = Task(user_id=user.id, kind="chat", source="qq", status="pending")
    db.add(later)
    db.flush()
    assert plan_after_chat(db, autonomous_settings, chat, proposal(), now=now) is None


def test_replanning_replaces_pending_and_proposal_cannot_set_agreement(
    db, autonomous_settings, scene_context
):
    _, project, chat, now = scene_context
    old = plan_after_chat(db, autonomous_settings, chat, proposal(), now=now)
    new = plan_after_chat(db, autonomous_settings, chat, proposal(), now=now)
    assert old.status == "cancelled" and old.id != new.id
    project.agreement = ""
    db.flush()
    assert plan_after_chat(db, autonomous_settings, chat, proposal(), now=now) is None
    assert new.status == "cancelled" and project.agreement == ""


def test_chat_for_old_project_cannot_plan_for_new_current_project(
    db, autonomous_settings, scene_context
):
    user, project, chat, now = scene_context
    project.is_current = False
    db.flush()
    db.add(Project(user_id=user.id, title="新的作品", agreement="另一个共识"))
    db.flush()
    assert plan_after_chat(db, autonomous_settings, chat, proposal(), now=now) is None


@pytest.mark.parametrize("enqueued", [False, True])
def test_memory_correction_invalidates_plan_before_queue_or_start(
    db, autonomous_settings, scene_context, enqueued
):
    user, _, _, now = scene_context
    plan = make_due(db, autonomous_settings, scene_context)
    if enqueued:
        enqueue_due_plans(db, autonomous_settings)
    user.memory_revision += 1
    db.flush()
    assert schedule_view(db, autonomous_settings, user, now).goal is None
    if enqueued:
        assert not autonomous_start_allowed(db, autonomous_settings, db.get(Task, plan.task_id))
    else:
        assert enqueue_due_plans(db, autonomous_settings) == 0
        assert plan.status == "cancelled"


def test_new_event_invalidates_plan_during_model_slot_wait(db, autonomous_settings, scene_context):
    user, _, _, now = scene_context
    plan = make_due(db, autonomous_settings, scene_context)
    enqueue_due_plans(db, autonomous_settings)
    task = db.get(Task, plan.task_id)
    task.status, task.started_at = "running", now
    db.flush()
    discard_pending_plans(db, user.id)
    assert plan.status == "cancelled" and task.status == "running"
    assert autonomous_start_allowed(db, autonomous_settings, task, now=now)
    # ModelBusy without a remote attempt resets the task to unstarted.
    task.status, task.started_at = "pending", None
    db.flush()
    assert not autonomous_start_allowed(db, autonomous_settings, task, now=now)


def test_attempt_limit_counts_cancelled_failed_and_late_started_legacy_tasks(
    db, autonomous_settings, scene_context
):
    user, project, chat, now = scene_context
    tasks = [
        Task(
            user_id=user.id,
            project_id=project.id,
            kind="scene",
            source="autonomous",
            status="cancelled",
            created_at=now + timedelta(seconds=1),
        ),
        Task(
            user_id=user.id,
            project_id=project.id,
            kind="scene",
            source="autonomous",
            status="failed",
            created_at=now - timedelta(hours=1),
            started_at=now,
        ),
        Task(
            user_id=user.id,
            project_id=project.id,
            kind="scene",
            source="qq",
            status="completed",
            created_at=now,
        ),
    ]
    db.add_all(tasks)
    db.flush()
    assert plan_after_chat(db, autonomous_settings, chat, proposal(), now=now) is None
    view = schedule_view(db, autonomous_settings, user, now)
    assert view.remaining_scenes == 0 and view.pause_reason == "limit_reached"
    user.last_active_at = now + timedelta(minutes=1)
    db.flush()
    assert schedule_view(db, autonomous_settings, user, now).remaining_scenes == 2


def test_new_event_cancels_only_unstarted_autonomous_work(db, autonomous_settings, scene_context):
    user, project, _, now = scene_context
    plan = make_due(db, autonomous_settings, scene_context)
    enqueue_due_plans(db, autonomous_settings)
    planned = db.get(Task, plan.task_id)
    tasks = [
        Task(
            user_id=user.id,
            project_id=project.id,
            kind="scene",
            source=source,
            status="pending",
            started_at=started,
        )
        for source, started in [
            ("autonomous", now),
            ("web", None),
            ("qq", None),
            ("autonomous", None),
        ]
    ]
    db.add_all(tasks)
    db.flush()
    discard_pending_plans(db, user.id)
    assert plan.status == "cancelled" and planned.status == "cancelled"
    assert [task.status for task in tasks] == ["pending", "pending", "pending", "cancelled"]
    assert user.last_active_at == now


def test_start_rechecks_deadline_settings_and_does_not_count_itself(
    db, autonomous_settings, scene_context
):
    user, _, _, now = scene_context
    settings = autonomous_settings.model_copy(update={"autonomous_scene_limit": 1})
    plan = make_due(db, settings, scene_context)
    enqueue_due_plans(db, settings)
    task = db.get(Task, plan.task_id)
    assert autonomous_start_allowed(db, settings, task, now=now)
    assert not autonomous_start_allowed(db, settings, task, now=now + timedelta(hours=24))
    disabled = settings.model_copy(update={"autonomy_enabled": False})
    assert not autonomous_start_allowed(db, disabled, task, now=now)
    task.started_at = now
    assert autonomous_start_allowed(db, disabled, task, now=now + timedelta(hours=25))


def test_schedule_pause_reasons_and_disabled_hide_goal(db, autonomous_settings, scene_context):
    user, project, _, now = scene_context
    make_due(db, autonomous_settings, scene_context)
    view = schedule_view(db, autonomous_settings, user, now + timedelta(hours=24))
    assert view.status == "paused" and view.pause_reason == "inactive"
    assert view.goal is None and view.due_at is None
    project.completed = True
    db.flush()
    assert schedule_view(db, autonomous_settings, user, now).pause_reason == "completed"
    disabled = autonomous_settings.model_copy(update={"autonomy_enabled": False})
    assert schedule_view(db, disabled, user, now).status == "disabled"
    assert enqueue_due_plans(db, disabled, now=now) == 0


def test_legacy_agreed_scene_ignores_delayed_opt_in_but_waits_for_user_chat(
    db, settings, scene_context
):
    user, project, _, now = scene_context
    task = Task(
        user_id=user.id,
        project_id=project.id,
        kind="scene",
        source="autonomous",
        status="pending",
        created_at=now,
        request={"goal": "即时共识场景"},
    )
    db.add(task)
    db.flush()
    assert not settings.autonomy_enabled
    assert autonomous_start_allowed(db, settings, task, now=now)
    pending = Task(user_id=user.id, kind="chat", source="qq", status="pending")
    db.add(pending)
    db.flush()
    assert not autonomous_start_allowed(db, settings, task, now=now)
    task.started_at = now
    assert autonomous_start_allowed(db, settings, task, now=now + timedelta(hours=25))


@pytest.mark.parametrize(
    "data",
    [
        {"goal": " ", "delay_minutes": 1},
        {"goal": "a" * 1001, "delay_minutes": 1},
        {"goal": "x", "delay_minutes": 0},
        {"goal": "x", "delay_minutes": 1441},
        {"goal": "x", "delay_minutes": 1, "kind": "rewrite"},
    ],
)
def test_scene_proposal_is_bounded(data):
    with pytest.raises(ValidationError):
        ScenePlanProposal.model_validate(data)
