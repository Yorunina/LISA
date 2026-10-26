from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    memory_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    __tablename__ = "invites"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), unique=True
    )
    consumed: Mapped[bool] = mapped_column(default=False, server_default="false")


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QQBinding(Base):
    __tablename__ = "qq_bindings"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    qq_id: Mapped[str] = mapped_column(String(32), unique=True)


class ContactPreference(Base):
    __tablename__ = "contact_preferences"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    timezone: Mapped[str] = mapped_column(
        String(128), default="Asia/Shanghai", server_default="Asia/Shanghai"
    )
    quiet_start: Mapped[int | None]
    quiet_end: Mapped[int | None]
    min_interval_minutes: Mapped[int] = mapped_column(default=240, server_default="240")
    max_per_day: Mapped[int] = mapped_column(default=2, server_default="2")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")


class ServiceCode(Base):
    __tablename__ = "service_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16))
    qq_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[str] = mapped_column(String(80))
    prompt: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)


class PersonaVersion(Base):
    __tablename__ = "persona_versions"

    persona_id: Mapped[UUID] = mapped_column(
        ForeignKey("personas.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StyleCorpus(Base):
    """Only temporary source storage; payload is erased on every terminal outcome."""

    __tablename__ = "style_corpora"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="ready")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    preview: Mapped[dict] = mapped_column(JSONB, default=dict)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collected_groups.id", ondelete="CASCADE"), index=True
    )
    source_revision: Mapped[int | None]


class CollectedGroup(Base):
    __tablename__ = "collected_groups"
    __table_args__ = (UniqueConstraint("bot_id", "qq_group_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    bot_id: Mapped[str] = mapped_column(String(32))
    qq_group_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    last_ordinal: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupGrant(Base):
    __tablename__ = "group_grants"

    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("collected_groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMessage(Base):
    __tablename__ = "group_messages"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id"),
        Index("ix_group_messages_group_time", "group_id", "sent_at", "id"),
        Index("ix_group_messages_member_time", "group_id", "qq_id", "sent_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("collected_groups.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(String(48))
    qq_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="text")
    ordinal: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StyleProfile(Base):
    __tablename__ = "style_profiles"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80))
    latest_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StyleProfileVersion(Base):
    __tablename__ = "style_profile_versions"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_profiles.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    prompt: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StyleApplication(Base):
    __tablename__ = "style_applications"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_profile_versions.id", ondelete="CASCADE")
    )


class ProjectStyleOverride(Base):
    __tablename__ = "project_style_overrides"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_profile_versions.id", ondelete="CASCADE")
    )


class ServiceEvent(Base):
    __tablename__ = "service_events"
    __table_args__ = (UniqueConstraint("bot_id", "message_id"),)

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    bot_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(48))
    qq_id: Mapped[str] = mapped_column(String(32), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="received")


class GuideNotice(Base):
    __tablename__ = "guide_notices"

    qq_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index(
            "uq_projects_current_user", "user_id", unique=True, postgresql_where=text("is_current")
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(16), default="novel")
    share_token: Mapped[str | None] = mapped_column(String(43), unique=True)
    is_current: Mapped[bool] = mapped_column(default=True, server_default="true")
    completed: Mapped[bool] = mapped_column(default=False, server_default="false")
    worldbook: Mapped[str] = mapped_column(Text, default="", server_default="")
    worldbook_entries: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    constraints: Mapped[str] = mapped_column(Text, default="", server_default="")
    characters: Mapped[str] = mapped_column(Text, default="", server_default="")
    author_prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    plot_state: Mapped[str] = mapped_column(Text, default="", server_default="")
    agreement: Mapped[str] = mapped_column(Text, default="", server_default="")
    preferences: Mapped[str] = mapped_column(Text, default="", server_default="")
    tentative: Mapped[str] = mapped_column(Text, default="", server_default="")
    last_creative_feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    retrieval_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Character(Base):
    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(60))
    prompt: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    known_information: Mapped[str] = mapped_column(Text, default="", server_default="")
    current_scene: Mapped[str] = mapped_column(Text, default="", server_default="")
    retrieval_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CharacterParticipation(Base):
    __tablename__ = "character_participations"

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL")
    )
    revision: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActivityPlan(Base):
    __tablename__ = "activity_plans"
    __table_args__ = (
        Index(
            "uq_activity_plans_pending_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    goal: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    memory_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))


class LifeWeekPlan(Base):
    __tablename__ = "life_week_plans"
    __table_args__ = (UniqueConstraint("user_id", "week_start"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    items: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("project_id", "position"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    position: Mapped[int]
    content: Mapped[dict] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(default=1, server_default="1")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (CheckConstraint(
        "user_id IS NOT NULL OR kind IN ('shared_analyze', 'shared_index')",
        name="ck_task_platform_kind",
    ),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24), default="chat")
    source: Mapped[str] = mapped_column(String(24), default="qq")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE", name="fk_tasks_project"), index=True
    )
    chapter_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL", name="fk_tasks_chapter")
    )
    request: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    input_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_of: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_tasks_retry")
    )


class ChapterVersion(Base):
    __tablename__ = "chapter_versions"

    chapter_id: Mapped[UUID] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(24))
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    event_key: Mapped[str | None] = mapped_column(String(96), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="text")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_excluded: Mapped[bool] = mapped_column(default=False, server_default="false")
    image_urls: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    image_description: Mapped[str] = mapped_column(Text, default="", server_default="")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    # Chat messages are transient processing input.  Agent-promoted memories
    # retain the task provenance without retaining the original message text.
    source_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    source_chapter_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL", name="fk_memories_source_chapter"),
        index=True,
    )
    source_version: Mapped[int | None]
    life_kind: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(Text, default="", server_default="")
    allow_chat: Mapped[bool] = mapped_column(default=True, server_default="true")
    allow_writing: Mapped[bool] = mapped_column(default=False, server_default="false")
    active: Mapped[bool] = mapped_column(default=True, server_default="true")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryUse(Base):
    __tablename__ = "memory_uses"

    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class LifeShare(Base):
    __tablename__ = "life_shares"

    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    memory_revision: Mapped[int]


class IndexState(Base):
    __tablename__ = "index_states"
    __table_args__ = (
        UniqueConstraint("source_kind", "source_id"),
        CheckConstraint("user_id IS NOT NULL OR source_kind = 'shared'", name="ck_index_public"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID]
    revision: Mapped[int]
    space: Mapped[str] = mapped_column(String(64))
    indexed_until: Mapped[int] = mapped_column(default=0, server_default="0")
    complete: Mapped[bool] = mapped_column(default=False, server_default="false")
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))


class IndexChunk(Base):
    __tablename__ = "index_chunks"

    state_id: Mapped[UUID] = mapped_column(
        ForeignKey("index_states.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]
    space: Mapped[str] = mapped_column(String(64), index=True)
    start: Mapped[int]
    end: Mapped[int]
    embedding: Mapped[list[float]] = mapped_column(Vector())


class RetrievalReference(Base):
    __tablename__ = "retrieval_references"

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID]
    revision: Mapped[int]
    space: Mapped[str] = mapped_column(String(64))
    chunk_index: Mapped[int]
    start: Mapped[int]
    end: Mapped[int]


class BudgetDay(Base):
    __tablename__ = "budget_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    budget_day: Mapped[date] = mapped_column(ForeignKey("budget_days.day"))
    state: Mapped[str] = mapped_column(String(24))
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    charged_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_outbound_messages_task_sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    chat_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    qq_id: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    interval_seconds: Mapped[float] = mapped_column(default=1.0, server_default="1")
    meme_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shared_entries.id", ondelete="SET NULL")
    )
    meme_revision: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    message_id: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SharedAsset(Base):
    __tablename__ = "shared_assets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    mime: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    uploader_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SharedEntry(Base):
    __tablename__ = "shared_entries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    tags: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    source_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    source_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    origin: Mapped[str] = mapped_column(String(16), default="contribution")
    origin_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    contributor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    editor_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shared_assets.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    human_edited: Mapped[bool] = mapped_column(default=False, server_default="false")
    allow_chat: Mapped[bool] = mapped_column(default=True, server_default="true")
    allow_writing: Mapped[bool] = mapped_column(default=False, server_default="false")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SharedRevision(Base):
    __tablename__ = "shared_revisions"

    entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("shared_entries.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(primary_key=True)
    editor_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SharedPreference(Base):
    __tablename__ = "shared_preferences"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    meme_mode: Mapped[str] = mapped_column(String(16), default="normal", server_default="normal")


class SharedMute(Base):
    __tablename__ = "shared_mutes"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("shared_entries.id", ondelete="CASCADE"), primary_key=True
    )
