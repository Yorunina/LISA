"""Persist a small scene intention; only create an author task when it is due."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.models import Account, ActivityPlan, ChatMessage, Persona, Project, QQBinding, Task
from lisa.settings import Settings
from lisa.works import create_writing_task, lock_account

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


class ScenePlanProposal(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    goal: str = Field(min_length=1, max_length=1000)
    delay_minutes: int = Field(ge=1, le=1440)


class ScheduleView(BaseModel):
    enabled: bool
    status: Literal["no_plan", "scheduled", "paused", "waiting", "disabled"]
    due_at: datetime | None = None
    goal: str | None = None
    project_id: UUID | None = None
    remaining_scenes: int
    pause_reason: str | None = None


def _current_project(db: Session, user_id: UUID) -> Project | None:
    return db.scalar(select(Project).where(Project.user_id == user_id, Project.is_current))


def _remaining_scenes(
    db: Session,
    settings: Settings,
    user: Account,
    project: Project | None,
    *,
    exclude_task: UUID | None = None,
) -> int:
    if project is None:
        return settings.autonomous_scene_limit
    query = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.user_id == user.id,
            Task.project_id == project.id,
            Task.kind == "scene",
            Task.source == "autonomous",
        )
    )
    if project.last_creative_feedback_at is not None:
        feedback_at = project.last_creative_feedback_at
        query = query.where(
            or_(
                Task.created_at >= feedback_at,
                Task.started_at >= feedback_at,
                Task.finished_at >= feedback_at,
                Task.status.in_(["pending", "running"]),
            )
        )
    if exclude_task is not None:
        query = query.where(Task.id != exclude_task)
    # Feedback cannot acknowledge unfinished output; keep in-flight attempts in the allowance.
    # Without a recorded feedback boundary, count existing history rather than grant a reset.
    return max(0, settings.autonomous_scene_limit - db.scalar(query))


def record_creative_feedback(
    db: Session,
    task: Task,
    source_ids: list[UUID],
    evidence: str,
) -> None:
    """Only adopted QQ understanding with a literal current-user source can advance the boundary."""
    if not evidence.strip() or task.kind != "chat" or task.source != "qq":
        return
    project = db.get(Project, task.project_id) if task.project_id else None
    if project is None or project.user_id != task.user_id or task.status != "completed":
        return
    messages = db.scalars(
        select(ChatMessage).where(
            ChatMessage.id.in_(source_ids),
            ChatMessage.task_id == task.id,
            ChatMessage.user_id == task.user_id,
            ChatMessage.role == "user",
            ChatMessage.deleted_at.is_(None),
            ChatMessage.context_excluded.is_(False),
        )
    )
    # A repeated quote in later casual chat must not move the feedback boundary forward.
    feedback_at = min(
        (message.sent_at for message in messages if evidence in message.content), default=None
    )
    if feedback_at is not None and (
        project.last_creative_feedback_at is None or feedback_at > project.last_creative_feedback_at
    ):
        project.last_creative_feedback_at = feedback_at


def _pause_reason(
    db: Session, user: Account, project: Project | None, now: datetime, remaining: int
) -> str | None:
    if (
        not user.active
        or db.get(QQBinding, user.id) is None
        or not db.scalar(select(Persona.id).where(Persona.user_id == user.id))
    ):
        return "account_unavailable"
    if user.last_active_at is None or now - user.last_active_at >= timedelta(hours=24):
        return "inactive"
    if project is None:
        return "no_project"
    if project.completed:
        return "completed"
    if not project.agreement.strip():
        return "no_agreement"
    if remaining == 0:
        return "limit_reached"
    return None


def has_unprocessed_chat(db: Session, user_id: UUID, except_task: UUID | None = None) -> bool:
    query = select(Task.id).where(
        Task.user_id == user_id, Task.kind == "chat", Task.status.in_(["pending", "running"])
    )
    if except_task is not None:
        query = query.where(Task.id != except_task)
    return db.scalar(query.limit(1)) is not None


def _writing_busy(db: Session, user_id: UUID) -> bool:
    return (
        db.scalar(
            select(Task.id)
            .where(
                Task.user_id == user_id,
                Task.kind.in_(["scene", "rewrite"]),
                Task.status.in_(["pending", "running"]),
            )
            .limit(1)
        )
        is not None
    )


def _cancel_pending_plans(db: Session, user_id: UUID) -> None:
    for plan in db.scalars(
        select(ActivityPlan).where(
            ActivityPlan.user_id == user_id, ActivityPlan.status == "pending"
        )
    ):
        plan.status = "cancelled"
    db.flush()


def discard_pending_plans(db: Session, user_id: UUID) -> None:
    """A deduplicated new QQ event invalidates intentions, including queued ones."""
    lock_account(db, user_id)
    _cancel_pending_plans(db, user_id)
    # Invalidate the intention even when its task is temporarily waiting for a model slot.
    for plan in db.scalars(
        select(ActivityPlan).where(
            ActivityPlan.user_id == user_id, ActivityPlan.status == "enqueued"
        )
    ):
        plan.status = "cancelled"
    now = datetime.now(UTC)
    for task in db.scalars(
        select(Task).where(
            Task.user_id == user_id,
            Task.source == "autonomous",
            Task.status == "pending",
            Task.started_at.is_(None),
        )
    ):
        task.status = "cancelled"
        task.finished_at = now
        task.error = "new_user_message"
    db.flush()


def handle_project_change(db: Session, user_id: UUID) -> None:
    """M04 has saved project flags under the account lock; leave started work to finish."""
    invalid_projects = select(Project.id).where(
        Project.user_id == user_id,
        or_(Project.is_current.is_(False), Project.completed.is_(True)),
    )
    for plan in db.scalars(
        select(ActivityPlan).where(
            ActivityPlan.user_id == user_id,
            ActivityPlan.project_id.in_(invalid_projects),
            ActivityPlan.status.in_(["pending", "enqueued"]),
        )
    ):
        plan.status = "cancelled"
    now = datetime.now(UTC)
    for task in db.scalars(
        select(Task).where(
            Task.user_id == user_id,
            Task.project_id.in_(invalid_projects),
            Task.kind.in_(["scene", "rewrite"]),
            Task.source == "autonomous",
            Task.status.in_(["pending", "running"]),
            Task.started_at.is_(None),
        )
    ):
        task.status, task.error, task.finished_at = "cancelled", "project_changed", now
    db.flush()


def plan_after_chat(
    db: Session,
    settings: Settings,
    task: Task,
    proposal: ScenePlanProposal | None,
    now: datetime | None = None,
) -> ActivityPlan | None:
    now = now or datetime.now(UTC)
    user = lock_account(db, task.user_id)
    _cancel_pending_plans(db, user.id)
    if not settings.autonomy_enabled or proposal is None:
        return None
    if task.kind != "chat" or task.source != "qq" or task.status != "completed":
        return None
    project = _current_project(db, user.id)
    remaining = _remaining_scenes(db, settings, user, project)
    if _pause_reason(db, user, project, now, remaining) is not None:
        return None
    if project.id != task.project_id:
        return None
    batch_activity = db.scalar(
        select(func.max(ChatMessage.sent_at)).where(
            ChatMessage.task_id == task.id, ChatMessage.role == "user"
        )
    )
    if batch_activity != user.last_active_at or has_unprocessed_chat(db, user.id, task.id):
        return None
    plan = ActivityPlan(
        user_id=user.id,
        project_id=project.id,
        goal=proposal.goal,
        activity_at=user.last_active_at,
        memory_revision=user.memory_revision,
        due_at=now
        + timedelta(
            minutes=max(proposal.delay_minutes, settings.autonomous_scene_interval_minutes)
        ),
    )
    db.add(plan)
    db.flush()
    return plan


def enqueue_due_plans(
    db: Session, settings: Settings, now: datetime | None = None, limit: int = 20
) -> int:
    if not settings.autonomy_enabled:
        return 0
    now = now or datetime.now(UTC)
    # Lock accounts in the same order used by chat/author; re-read after each lock.
    plan_ids = list(
        db.scalars(
            select(ActivityPlan.id)
            .where(
                ActivityPlan.status == "pending",
                ActivityPlan.due_at <= now,
            )
            .order_by(ActivityPlan.due_at, ActivityPlan.id)
            .limit(limit)
        )
    )
    created = 0
    for plan_id in plan_ids:
        plan = db.get(ActivityPlan, plan_id)
        user = lock_account(db, plan.user_id)
        db.refresh(plan)
        if plan.status != "pending":
            continue
        project = _current_project(db, user.id)
        remaining = _remaining_scenes(db, settings, user, project)
        if (
            _pause_reason(db, user, project, now, remaining) is not None
            or project.id != plan.project_id
            or user.last_active_at != plan.activity_at
            or user.memory_revision != plan.memory_revision
            or has_unprocessed_chat(db, user.id)
        ):
            plan.status = "cancelled"
            continue
        if _writing_busy(db, user.id):
            continue
        task = create_writing_task(
            db, user, project, "scene", plan.goal, source="autonomous", notify_qq=False
        )
        task.request = {
            **task.request,
            "plan_id": str(plan.id),
            "activity_at": plan.activity_at.isoformat(),
            "memory_revision": plan.memory_revision,
        }
        plan.task_id = task.id
        plan.status = "enqueued"
        created += 1
    db.flush()
    return created


def autonomous_start_allowed(
    db: Session, settings: Settings, task: Task, now: datetime | None = None
) -> bool:
    if task.started_at is not None:
        return True
    now = now or datetime.now(UTC)
    user = db.get(Account, task.user_id)
    project = _current_project(db, user.id)
    remaining = _remaining_scenes(db, settings, user, project, exclude_task=task.id)
    if (
        _pause_reason(db, user, project, now, remaining) is not None
        or project.id != task.project_id
        or has_unprocessed_chat(db, user.id)
    ):
        return False
    if "plan_id" not in task.request:
        return True  # An immediate QQ-agreed scene does not require delayed-plan opt-in.
    plan = db.get(ActivityPlan, UUID(task.request["plan_id"]))
    return (
        settings.autonomy_enabled
        and plan.status == "enqueued"
        and plan.task_id == task.id
        and plan.user_id == user.id
        and task.request["memory_revision"] == user.memory_revision
        and task.request.get("activity_at") == user.last_active_at.isoformat()
    )


def schedule_view(
    db: Session, settings: Settings, user: Account, now: datetime | None = None
) -> ScheduleView:
    now = now or datetime.now(UTC)
    project = _current_project(db, user.id)
    remaining = _remaining_scenes(db, settings, user, project)
    view = ScheduleView(
        enabled=settings.autonomy_enabled, status="no_plan", remaining_scenes=remaining
    )
    if not settings.autonomy_enabled:
        view.status = "disabled"
        return view
    reason = _pause_reason(db, user, project, now, remaining)
    if reason:
        view.status, view.pause_reason = "paused", reason
        return view
    plan = db.scalar(
        select(ActivityPlan).where(
            ActivityPlan.user_id == user.id,
            ActivityPlan.status == "pending",
            ActivityPlan.project_id == project.id,
            ActivityPlan.activity_at == user.last_active_at,
            ActivityPlan.memory_revision == user.memory_revision,
        )
    )
    if plan is None or has_unprocessed_chat(db, user.id):
        return view
    view.status = "waiting" if plan.due_at <= now and _writing_busy(db, user.id) else "scheduled"
    view.due_at, view.goal, view.project_id = plan.due_at, plan.goal, plan.project_id
    return view


@router.get("", response_model=ScheduleView)
def get_schedule(request: Request, db: DB):
    user = current_user(request, db)
    return schedule_view(db, request.app.state.settings, user)
