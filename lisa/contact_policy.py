"""Explicit contact settings and the common generation/delivery eligibility check."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.interaction import interaction_view
from lisa.models import (
    Account,
    ContactPreference,
    OutboundMessage,
    Persona,
    QQBinding,
    Task,
)
from lisa.scheduling import has_unprocessed_chat
from lisa.settings import Settings
from lisa.works import lock_account

router = APIRouter(prefix="/api/contact-settings", tags=["contact"])
User = Annotated[Account, Depends(current_user)]


class ContactSettings(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)
    enabled: bool = False
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)
    quiet_start: int | None = Field(default=None, ge=0, le=1439)
    quiet_end: int | None = Field(default=None, ge=0, le=1439)
    min_interval_minutes: int = Field(default=240, ge=60, le=1440)
    max_per_day: int = Field(default=2, ge=1, le=5)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请选择有效的 IANA 时区，例如 Asia/Shanghai。") from None
        return value

    @model_validator(mode="after")
    def quiet_hours_pair(self):
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("免打扰开始和结束时间须同时填写或同时留空。")
        if self.quiet_start is not None and self.quiet_start == self.quiet_end:
            raise ValueError("免打扰开始和结束时间不能相同。")
        return self


class ContactSettingsView(ContactSettings):
    revision: int = 1
    available: bool
    observations: dict


def _view(db: Session, user_id: UUID, settings: Settings) -> ContactSettingsView:
    item = db.get(ContactPreference, user_id)
    values = (
        ContactSettings.model_validate(item).model_dump()
        if item
        else ContactSettings().model_dump()
    )
    return ContactSettingsView(
        **values,
        revision=item.revision if item else 1,
        available=settings.proactive_enabled,
        observations=interaction_view(db, user_id),
    )


@router.get("", response_model=ContactSettingsView)
def get_settings(request: Request, db: DB, user: User):
    return _view(db, user.id, request.app.state.settings)


@router.post("", response_model=ContactSettingsView)
def save_settings(body: ContactSettings, request: Request, db: DB, user: User):
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号已不可用。")
    item = db.get(ContactPreference, user.id)
    values = body.model_dump()
    if item is None:
        item = ContactPreference(user_id=user.id, **values)
        db.add(item)
    elif any(getattr(item, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(item, key, value)
        item.revision += 1
    db.commit()
    return _view(db, user.id, request.app.state.settings)


def contact_snapshot(db: Session, user_id: UUID) -> dict:
    """Metadata only: no copy of a user's message or inferred schedule."""
    user = db.get(Account, user_id)
    preferences = db.get(ContactPreference, user_id)
    persona_version = db.scalar(select(Persona.version).where(Persona.user_id == user_id))
    return {
        "activity_at": user.last_active_at.isoformat() if user and user.last_active_at else None,
        "memory_revision": user.memory_revision if user else None,
        "preferences_revision": preferences.revision if preferences else 0,
        "persona_version": persona_version,
    }


def contact_block_reason(
    db: Session,
    settings: Settings,
    user_id: UUID,
    *,
    task: Task | None = None,
    now: datetime | None = None,
) -> str | None:
    """Hold account lock; observed hours can delay base eligibility by at most two hours."""
    now = now or datetime.now(UTC)
    if not settings.proactive_enabled:
        return "disabled"
    user = db.get(Account, user_id)
    if (
        user is None
        or not user.active
        or db.get(QQBinding, user_id) is None
        or not db.scalar(select(Persona.id).where(Persona.user_id == user_id))
    ):
        return "account_unavailable"
    preferences = db.get(ContactPreference, user_id)
    if preferences is None or not preferences.enabled:
        return "user_disabled"
    if user.last_active_at is None or now - user.last_active_at >= timedelta(hours=24):
        return "inactive"
    if has_unprocessed_chat(db, user_id):
        return "unprocessed_chat"
    snapshot = contact_snapshot(db, user_id)
    if db.scalar(
        select(OutboundMessage.id)
        .join(Task, Task.id == OutboundMessage.task_id)
        .where(
            Task.user_id == user_id,
            Task.kind == "chat",
            OutboundMessage.status.in_(["pending", "sending"]),
        )
        .limit(1)
    ):
        return "unprocessed_chat"
    if task is not None and (
        task.user_id != user_id
        or task.kind != "contact"
        or task.source != "autonomous"
        or any(task.request.get(key) != value for key, value in snapshot.items())
    ):
        return "snapshot_changed"
    if preferences.quiet_start is not None:
        local = now.astimezone(ZoneInfo(preferences.timezone))
        minute = local.hour * 60 + local.minute
        start, end = preferences.quiet_start, preferences.quiet_end
        quiet = start <= minute < end if start < end else minute >= start or minute < end
        if quiet:
            return "quiet_hours"

    others = [Task.user_id == user_id, Task.kind == "contact", Task.source == "autonomous"]
    if task is not None:
        others.append(Task.id != task.id)
    # A delayed delivery is a recent contact even when the job was queued long ago.
    sent_at = db.scalar(
        select(func.max(OutboundMessage.finished_at))
        .join(
            Task,
            Task.id == OutboundMessage.task_id,
        )
        .where(*others, OutboundMessage.status.in_(["sent", "unknown"]))
    )
    queued_at = db.scalar(select(func.max(Task.created_at)).where(*others))
    last_contact = max(
        time for time in (user.last_active_at, sent_at, queued_at) if time is not None
    )
    if now - last_contact < timedelta(minutes=preferences.min_interval_minutes):
        return "min_interval"
    since = now - timedelta(hours=24)
    count = db.scalar(
        select(func.count(Task.id))
        .outerjoin(
            OutboundMessage,
            OutboundMessage.task_id == Task.id,
        )
        .where(
            *others,
            or_(
                Task.created_at > since,
                OutboundMessage.status.in_(["sent", "unknown"])
                & (OutboundMessage.finished_at > since),
            ),
        )
    )
    if count >= preferences.max_per_day:
        return "daily_limit"
    if db.scalar(
        select(Task.id)
        .where(
            *others,
            Task.request["activity_at"].astext == snapshot["activity_at"],
        )
        .limit(1)
    ):
        return "feedback_used"
    preferred = interaction_view(db, user_id, now=now)["preferred_hours"]
    if preferred:
        local_hour = now.astimezone(ZoneInfo(preferences.timezone)).hour
        base_time = last_contact + timedelta(minutes=preferences.min_interval_minutes)
        if not preferred["start"] <= local_hour < preferred["end"] and now < base_time + timedelta(
            hours=2
        ):
            return "preferred_hours"
    return None
