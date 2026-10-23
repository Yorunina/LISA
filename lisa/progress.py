"""One optional, current saved result inside an otherwise ordinary proactive conversation."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from lisa.models import Chapter, ChapterVersion, OutboundMessage, Project, Task
from lisa.works import document_text


class ProgressMention(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    task_id: UUID
    evidence: str = Field(min_length=1, max_length=240)


def _saved_results(user_id):
    return (
        select(Task, Chapter, Project)
        .join(ChapterVersion, ChapterVersion.task_id == Task.id)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .join(Project, Project.id == Chapter.project_id)
        .where(
            Task.user_id == user_id,
            Task.project_id == Project.id,
            Task.chapter_id == Chapter.id,
            Task.source == "autonomous",
            Task.kind.in_(["scene", "rewrite"]),
            Task.status == "completed",
            Task.adopted_at.is_not(None),
            Project.user_id == user_id,
            Project.is_current.is_(True),
            Chapter.version == ChapterVersion.version,
        )
    )


def progress_context(db, user_id, *, now=None) -> tuple[dict | None, str]:
    """Read existing adoption/outbox records; completion never queues a contact by itself."""
    now = now or datetime.now(UTC)
    last_contact = db.scalar(
        select(Task)
        .join(OutboundMessage, OutboundMessage.task_id == Task.id)
        .where(
            Task.user_id == user_id,
            Task.kind == "contact",
            OutboundMessage.status.in_(["sent", "unknown"]),
        )
        .order_by(OutboundMessage.finished_at.desc(), OutboundMessage.created_at.desc())
        .limit(1)
    )
    if last_contact and (last_contact.input_snapshot or {}).get("progress"):
        return None, ""  # Leave at least one ordinary contact between progress shares.
    result = db.execute(
        _saved_results(user_id)
        .where(Task.adopted_at > now - timedelta(hours=24), Task.adopted_at <= now)
        .order_by(Task.adopted_at.desc(), Task.id.desc())
        .limit(1)
    ).first()
    if result is None:
        return None, ""
    task, chapter, project = result
    already_mentioned = db.scalar(
        select(OutboundMessage.id)
        .join(Task, Task.id == OutboundMessage.task_id)
        .where(
            Task.user_id == user_id,
            Task.kind == "contact",
            Task.input_snapshot["progress"]["task_id"].astext == str(task.id),
            OutboundMessage.status.in_(["pending", "sending", "sent", "unknown"]),
        )
        .limit(1)
    )
    if already_mentioned:
        return None, ""
    reference = {
        "task_id": str(task.id),
        "project_id": str(project.id),
        "chapter_id": str(chapter.id),
        "version": chapter.version,
    }
    context = "\n可选的已保存创作成果：" + json.dumps(
        {
            **reference,
            "project_title": project.title,
            "project_kind": project.kind,
            "chapter_title": chapter.title,
            "adopted_at": task.adopted_at.isoformat(),
            "excerpt": document_text(chapter.content)[:200],
        },
        ensure_ascii=False,
    )
    context += (
        "\n这只是已正式保存正文的一小段，不能据此宣称完成整部作品或猜测片段之外的剧情。"
        "本次仍以日常交流为主，确有自然关联时最多用一句顺带分享，也可以完全不提。"
        "不是进度播报，不催阅读或反馈。若提及，progress_mention.task_id使用提供的id，"
        "evidence逐字引用reply中提到成果的那一句；不提则null。"
        "小说中的事件属于作品人物，不是LISA亲历，不记入life_events。"
        "日记片段也不重复提取为新增生活事件；已有生活事件的分享用life_mentions。"
    )
    return reference, context


def resolve_progress(mention: ProgressMention | None, provided: dict | None, reply: str):
    if mention is None:
        return None
    if (
        provided is None
        or str(mention.task_id) != provided["task_id"]
        or mention.evidence not in reply
    ):
        raise ValueError("Progress mention must refer to the supplied result and actual reply")
    return provided


def progress_is_current(db, user_id, reference: dict | None) -> bool:
    if reference is None:
        return True
    return (
        db.execute(
            _saved_results(user_id).where(
                Task.id == UUID(reference["task_id"]),
                Project.id == UUID(reference["project_id"]),
                Chapter.id == UUID(reference["chapter_id"]),
                Chapter.version == reference["version"],
            )
        ).first()
        is not None
    )
