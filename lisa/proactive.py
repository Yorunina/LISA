"""Bounded daily-life contact, using the existing task ledger and QQ outbox."""

from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.chat_style import expression_guide
from lisa.contact_policy import contact_block_reason, contact_snapshot
from lisa.life import (
    LIFE_INSTRUCTIONS,
    LifeCandidate,
    LifeMention,
    adopt_life_events,
    queue_life_shares,
)
from lisa.memory import memory_context, record_chat_references
from lisa.model_calls import (
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    ModelInputTooLarge,
    generate_structured,
    trim_history,
)
from lisa.models import (
    Account,
    ContactPreference,
    OutboundMessage,
    Persona,
    Project,
    QQBinding,
    Task,
)
from lisa.progress import ProgressMention, progress_context, progress_is_current, resolve_progress
from lisa.settings import Settings
from lisa.sharing import chapter_link
from lisa.style_profiles import resolve_personality_style
from lisa.topics import DefaultDesign, adopt_default_design, topic_context


class ContactReply(BaseModel):
    provider_output_default: ClassVar[bool] = True
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reply: str = Field(min_length=1, max_length=800)
    meme_id: UUID | None = None
    life_events: list[LifeCandidate] = Field(default_factory=list, max_length=3)
    life_mentions: list[LifeMention] = Field(default_factory=list, max_length=3)
    default_design: DefaultDesign | None = None
    progress_mention: ProgressMention | None = None


def enqueue_due_contacts(db: Session, settings: Settings, *, now=None, limit=20) -> int:
    if not settings.proactive_enabled:
        return 0
    from lisa.queue import enqueue_contact

    now = now or datetime.now(UTC)
    candidates = db.scalars(
        select(Account.id)
        .join(ContactPreference, ContactPreference.user_id == Account.id)
        .where(
            Account.active.is_(True),
            Account.last_active_at > now - timedelta(hours=24),
            ContactPreference.enabled.is_(True),
        )
        .order_by(Account.last_active_at, Account.id)
    ).all()
    count = 0
    for user_id in candidates:
        db.scalar(select(Account).where(Account.id == user_id).with_for_update())
        if contact_block_reason(db, settings, user_id, now=now):
            continue
        task = Task(
            user_id=user_id,
            kind="contact",
            source="autonomous",
            status="pending",
            request=contact_snapshot(db, user_id),
        )
        db.add(task)
        db.flush()
        task.queue_job_id = enqueue_contact(db, task.id, user_id)
        count += 1
        if count >= limit:
            break
    return count


async def generate_contact(settings, engine, task_id, user_id, instructions, messages):
    return await generate_structured(
        settings, engine, task_id, user_id, instructions, messages, schema=ContactReply
    )


async def execute_contact(settings: Settings, engine, task_id: str, *, generate=None) -> None:
    task_id = UUID(str(task_id))
    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        if task is None or task.kind != "contact" or task.status != "pending":
            return
        user = db.scalar(select(Account).where(Account.id == task.user_id).with_for_update())
        if user is None:
            return
        db.refresh(task, with_for_update=True)
        if task.status != "pending":
            return
        reason = contact_block_reason(db, settings, task.user_id, task=task)
        if reason:
            task.status, task.error, task.finished_at = "cancelled", reason, datetime.now(UTC)
            return
        user_id = task.user_id
        persona = db.scalar(select(Persona).where(Persona.user_id == user_id))
        style_card = resolve_personality_style(
            db,
            user_id,
            scene="casual",
            fallback=persona.prompt if persona else "",
        )
        # Proactive contact has no retained chat history to replay.  It uses
        # explicit memories and current project facts only.
        history = []
        instructions = (
            "你是该用户独立的文学创作伙伴 LISA。这是一条温和简短的主动私聊，"
            "不是对新消息的回复。以自己的虚构日常、兴趣或感受开启轻松话题；"
            "不催回复、不追问先前未回答的问题，不把写作进展当作唯一话题。"
            "避开最近刚谈过的重复内容，可以自然换个话题。"
            "你及生活均为虚构设定；用户的推测记忆不是事实，不臆测用户睡眠、工作或情绪。"
            "不宣称执行了未保存的创作、不将待定意见变成共识、不发起写作任务。"
            "只有提供可选已保存成果时才可提及创作进展，否则progress_mention必须null。"
            f"当前 UTC 时间：{datetime.now(UTC).isoformat()}。\n"
            f"用户选定的性格名称：{persona.name}\n交流风格卡：\n{style_card['prompt']}"
            + "\n" + expression_guide() + LIFE_INSTRUCTIONS
            + "\nlife_mentions只列本次reply明确提及的已提供生活记忆id，evidence逐字来自reply。"
        )
        memory_ids = []
        project = db.scalar(select(Project).where(Project.user_id == user_id, Project.is_current))
        topics = topic_context(project)
        project_id = project.id if project else None
        provided_tentative = project.tentative if topics else ""
        optional_context = topics
        provided_progress, progress_text = progress_context(db, user_id)
        optional_context += progress_text
        memories = memory_context(
            db, user_id, purpose="chat", max_chars=1200, references=memory_ids
        )
        from lisa.shared_retrieval import sent_meme_context
        messages = [
            {"role": item.role, "content": f"[时间 {item.sent_at.isoformat()}]\n{item.content}"
             + sent_meme_context(db, item)}
            for item in history
        ] + [{"role": "user", "content": "请分享一条自然的生活话题，不要求我立即回应。"}]
        task.status, task.started_at = "running", datetime.now(UTC)
        from lisa.shared_retrieval import recall_shared
        public_context = recall_shared(
            db, settings, user_id, "chat", ""
        )
        optional_context += public_context.text
    error = None
    result = None
    try:
        try:
            messages = trim_history(
                settings,
                instructions + optional_context + memories,
                messages,
                len(history),
                schema=ContactReply,
            )
            instructions += optional_context + memories
        except ModelInputTooLarge:
            from lisa.retrieval import Context
            public_context = Context()
            memory_ids = []
            provided_tentative = ""
            provided_progress = None
            messages = trim_history(
                settings, instructions, messages, len(history), schema=ContactReply
            )
        generator = generate or generate_contact
        result = ContactReply.model_validate(
            await generator(settings, engine, task_id, user_id, instructions, messages)
        )
        progress = resolve_progress(result.progress_mention, provided_progress, result.reply)
    except BudgetExceeded:
        error = "budget_exhausted"
    except ModelBusy:
        error = "model_busy"
    except ModelCallUnknown:
        error = "model_unknown"
    except ModelInputTooLarge:
        error = "input_too_large"
    except (ModelCallFailed, ValueError):
        error = "model_failed"
    with Session(engine) as db, db.begin():
        db.scalar(select(Account).where(Account.id == user_id).with_for_update())
        task = db.get(Task, task_id, with_for_update=True)
        if task is None or task.status != "running":
            return
        task.finished_at = datetime.now(UTC)
        if error:
            task.status, task.error = "failed", error
            return  # An unsolicited contact failure must not produce an error message to QQ.
        reason = contact_block_reason(db, settings, user_id, task=task)
        if reason:
            task.status, task.error = "cancelled", reason
            return
        if not progress_is_current(db, user_id, progress):
            task.status, task.error = "cancelled", "progress_changed"
            return
        from lisa.retrieval import record_references
        from lisa.shared_retrieval import select_meme, shared_references_current
        if not shared_references_current(db, public_context, user_id):
            task.status, task.error = "cancelled", "shared_source_changed"
            return
        record_references(db, task_id, public_context)
        meme = select_meme(db, user_id, result.meme_id, public_context)
        record_chat_references(db, task_id, memory_ids)
        adopt_default_design(
            db, user_id, project_id, result.default_design, provided_tentative=provided_tentative
        )
        task.input_snapshot = {
            "life_events": [item.model_dump(mode="json") for item in result.life_events],
            "progress": progress,
        }
        queue_life_shares(
            db,
            user_id,
            task_id,
            [
                item.memory_id
                for item in result.life_mentions
                if item.memory_id in memory_ids and item.evidence in result.reply
            ],
        )
        task.status = "completed"
        binding = db.get(QQBinding, user_id)
        reply = result.reply
        if progress:
            link = chapter_link(db, settings, user_id, UUID(progress["chapter_id"]))
            if link:
                reply += "\n\n" + link
        db.add(
            OutboundMessage(
                user_id=user_id,
                task_id=task_id,
                qq_id=binding.qq_id,
                content=reply,
                meme_id=meme.id if meme else None,
                meme_revision=meme.revision if meme else None,
                status="pending",
            )
        )


def adopt_delivered_contact(db: Session, message: OutboundMessage) -> None:
    """Only confirmed delivery adopts newly generated LISA life events."""
    task = db.get(Task, message.task_id)
    user = db.scalar(select(Account).where(Account.id == message.user_id).with_for_update())
    changed = user.memory_revision != task.request["memory_revision"] or not progress_is_current(
        db, user.id, (task.input_snapshot or {}).get("progress")
    )
    if not changed:
        ids = adopt_life_events(
            db,
            user.id,
            [LifeCandidate.model_validate(item) for item in task.input_snapshot["life_events"]],
            message.content,
            source_task_id=task.id,
        )
        queue_life_shares(db, user.id, task.id, ids)
