"""Adopted LISA life events shared by chat and diary writing."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from lisa.models import (
    Chapter,
    ChapterVersion,
    ChatMessage,
    LifeShare,
    Memory,
    MemoryUse,
    Project,
    Task,
)

LIFE_INSTRUCTIONS = (
    "\n共享生活时间线记录 LISA 已正式采用的虚拟经历，不是用户真实事实。"
    "life_events 仅提出本轮正式采用正文中的新增 LISA 事件，最多 3 条。"
    "nature 明确区分 event 已发生小事、dream 做过的梦、plan 计划、imagination 想象、"
    "character 小说人物经历、background_change 稳定背景变更；后四类不得记成已发生事件。"
    "evidence 必须逐字摘自本轮正式采用的回复或正文，content 是简要经历；"
    "occurred_at 使用含时区 ISO 时间且不得在未来。梦境只表示做过这个梦。"
    "沿用账号共享的最新生活时间线；shared_at 为空表示没有成功分享记录，"
    "不得声称之前已告诉用户。搬家、改职业等稳定背景变化须先作为想法交流。"
)


class LifeCandidate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(min_length=1, max_length=1000)
    nature: Literal["event", "dream", "plan", "imagination", "character", "background_change"]
    occurred_at: AwareDatetime
    evidence: str = Field(min_length=1, max_length=2000)


class LifeMention(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    memory_id: UUID
    evidence: str = Field(min_length=1, max_length=2000)


def adopt_life_events(
    db: Session,
    user_id: UUID,
    candidates: list[LifeCandidate],
    adopted_text: str,
    source_message_id: UUID | None = None,
    chapter: ChapterVersion | None = None,
) -> list[UUID]:
    """Called in the adoption transaction with its account lock, after saving the source."""
    from lisa.works import document_text

    if (source_message_id is None) == (chapter is None):
        return []
    source_task_id = None
    if source_message_id:
        # A deleted life memory must not be recreated from the same generated
        # assistant source, even when that source is a hidden multi-part reply.
        if db.scalar(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.source_message_id == source_message_id,
                Memory.revision > 1,
            )
        ):
            return []
        source = db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.id == source_message_id,
                ChatMessage.user_id == user_id,
                ChatMessage.role == "assistant",
                ChatMessage.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        if source is None or source.content != adopted_text:
            return []
        source_task_id = source.task_id
    else:
        saved = db.scalar(
            select(ChapterVersion)
            .join(
                Chapter,
                Chapter.id == ChapterVersion.chapter_id,
            )
            .join(Project, Project.id == Chapter.project_id)
            .where(
                ChapterVersion.chapter_id == chapter.chapter_id,
                ChapterVersion.version == chapter.version,
                Project.user_id == user_id,
                Project.kind == "diary",
            )
            .execution_options(populate_existing=True)
        )
        if saved is None or document_text(saved.content) != adopted_text:
            return []
        if db.scalar(
            select(Memory.id).where(
                Memory.user_id == user_id,
                Memory.source_chapter_id == saved.chapter_id,
                Memory.source_version == saved.version,
                Memory.revision > 1,
            )
        ):
            return []
        chapter = saved
    adopted = []
    now = datetime.now(UTC)
    for candidate in candidates:
        if (
            candidate.nature not in {"event", "dream"}
            or candidate.occurred_at > now
            or candidate.evidence not in adopted_text
        ):
            continue
        content = candidate.content
        if candidate.nature == "dream" and not content.startswith("做过一个梦："):
            content = "做过一个梦：" + content
        item = db.scalar(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.kind == "life",
                Memory.active.is_(True),
                Memory.content == content,
                Memory.occurred_at == candidate.occurred_at,
            )
        )
        if item is None:
            item = Memory(
                user_id=user_id,
                project_id=None,
                kind="life",
                life_kind=candidate.nature,
                content=content,
                occurred_at=candidate.occurred_at,
                allow_chat=True,
                allow_writing=True,
                source_message_id=source_message_id,
                source_chapter_id=chapter.chapter_id if chapter else None,
                source_version=chapter.version if chapter else None,
            )
            db.add(item)
            db.flush()
        if source_task_id and db.get(MemoryUse, (item.id, source_task_id)) is None:
            db.add(MemoryUse(memory_id=item.id, task_id=source_task_id))
        if item.id not in adopted:
            adopted.append(item.id)
    db.flush()
    return adopted


def queue_life_shares(db: Session, user_id: UUID, task_id: UUID, ids: list[UUID]) -> None:
    task = db.get(Task, task_id)
    if task is None or task.user_id != user_id:
        return
    valid = db.scalars(
        select(Memory).where(
            Memory.id.in_(ids),
            Memory.user_id == user_id,
            Memory.kind == "life",
            Memory.active.is_(True),
            Memory.allow_chat.is_(True),
        )
    )
    for item in valid:
        if db.get(LifeShare, (item.id, task_id)) is None:
            db.add(LifeShare(memory_id=item.id, task_id=task_id, memory_revision=item.revision))
    db.flush()


def mark_life_shared(db: Session, task_id: UUID, sent_at: datetime) -> None:
    """The delivery caller invokes this only on confirmed success, never on enqueue."""
    db.execute(
        update(Memory)
        .where(
            LifeShare.memory_id == Memory.id,
            LifeShare.task_id == task_id,
            Memory.active.is_(True),
            Memory.kind == "life",
            Memory.allow_chat.is_(True),
            Memory.revision == LifeShare.memory_revision,
            or_(Memory.shared_at.is_(None), Memory.shared_at > sent_at),
        )
        .values(shared_at=sent_at)
        .execution_options(synchronize_session=False)
    )
    db.flush()
