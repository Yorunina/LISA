import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from procrastinate.exceptions import JobRetry
from procrastinate.retry import RetryDecision
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lisa.dialogue import (
    MEMORY_INSTRUCTIONS,
    Conversation,
    Dialogue,
    apply_decision,
    creative_context,
    generate_conversation,
    generate_dialogue,
)
from lisa.interaction import interaction_context
from lisa.life import LIFE_INSTRUCTIONS, adopt_life_events, mark_life_shared, queue_life_shares
from lisa.memory import (
    adopt_candidates,
    memory_context,
    record_chat_references,
    usable_chat_clause,
)
from lisa.model_calls import (
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    ModelInputTooLarge,
    trim_history,
)
from lisa.models import (
    Account,
    ChatMessage,
    OutboundMessage,
    Persona,
    Project,
    QQBinding,
    ServiceEvent,
    Task,
)
from lisa.queue import enqueue_chat
from lisa.recall import ContextChanged, check_memory_revision, retrieve_context
from lisa.retrieval import Context, record_references
from lisa.scheduling import discard_pending_plans, plan_after_chat, record_creative_feedback
from lisa.settings import Settings
from lisa.sharing import chapter_link, sharing_context
from lisa.vision import (
    MAX_IMAGES,
    clear_image_inputs,
    image_url_allowed,
    message_text,
    prepare_chat_images,
)

logger = logging.getLogger(__name__)


def ingest_message(
    db: Session,
    *,
    bot_id: str,
    message_id: str,
    qq_id: str,
    sent_at: datetime,
    content: str,
    kind: str = "text",
    image_urls: list[str] | None = None,
) -> UUID | None:
    """Persist one initialized user's event and its queue job in a single transaction."""
    binding = db.scalar(select(QQBinding).where(QQBinding.qq_id == qq_id))
    if binding is None:
        return None
    user = db.scalar(select(Account).where(Account.id == binding.user_id).with_for_update())
    if user is None or not user.active:
        return None
    persona = db.scalar(select(Persona).where(Persona.user_id == user.id))
    if persona is None:
        return None
    now = datetime.now(UTC)
    if sent_at > now + timedelta(seconds=60):
        return None
    key = f"{bot_id}:{message_id}"
    if db.get(ServiceEvent, key) or db.scalar(
        select(ChatMessage.id).where(ChatMessage.event_key == key)
    ):
        return None
    discard_pending_plans(db, user.id)
    user.last_active_at = max(user.last_active_at, sent_at) if user.last_active_at else sent_at
    task = db.scalar(
        select(Task)
        .where(Task.user_id == user.id, Task.kind == "chat", Task.status == "pending")
        .order_by(Task.queue_job_id.desc())
    )
    # A queued batch is bounded; subsequent messages form another sequential task.
    if (
        task
        and db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.task_id == task.id)
        )
        >= 20
    ):
        task = None
    if task is None:
        task = Task(user_id=user.id, kind="chat", source="qq", status="pending")
        db.add(task)
        db.flush()
        task.queue_job_id = enqueue_chat(db, task.id, user.id)
    db.add(
        ChatMessage(
            user_id=user.id,
            task_id=task.id,
            event_key=key,
            role="user",
            content=content,
            kind=kind,
            image_urls=[
                url for url in (image_urls or [])[: MAX_IMAGES + 1] if image_url_allowed(url)
            ],
            sent_at=sent_at,
        )
    )
    db.flush()
    return task.id


def _outbound(db: Session, task: Task, qq_id: str, content: str) -> None:
    if not db.scalar(select(OutboundMessage.id).where(OutboundMessage.task_id == task.id)):
        db.add(
            OutboundMessage(
                user_id=task.user_id,
                task_id=task.id,
                qq_id=qq_id,
                content=content,
                status="pending",
            )
        )


async def execute_chat(settings: Settings, engine, task_id: str, *, generate=None) -> None:
    task_id = UUID(str(task_id))
    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        if task is None or task.status != "pending":
            return  # Persisted running/finished attempts are never blindly regenerated.
        user = db.scalar(select(Account).where(Account.id == task.user_id).with_for_update())
        task = db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != "pending":
            return
        binding = db.get(QQBinding, task.user_id)
        persona = db.scalar(select(Persona).where(Persona.user_id == task.user_id))
        if user is None or not user.active or binding is None or persona is None:
            task.status, task.error = "failed", "account_unavailable"
            task.finished_at = datetime.now(UTC)
            clear_image_inputs(db, task_id)
            return
        prior = db.scalar(
            select(Task.id).where(
                Task.user_id == user.id,
                Task.kind == "chat",
                Task.queue_job_id < task.queue_job_id,
                Task.status.in_(["pending", "running"]),
            )
        )
        if prior:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3}))
        batch = db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.task_id == task.id,
                ChatMessage.role == "user",
                usable_chat_clause(),
            )
            .order_by(ChatMessage.sent_at, ChatMessage.created_at)
        ).all()
        if not batch:
            task.status, task.error = "failed", "empty_input"
            task.finished_at = datetime.now(UTC)
            clear_image_inputs(db, task_id)
            return
        history = db.scalars(
            select(ChatMessage)
            .join(Task, ChatMessage.task_id == Task.id)
            .where(
                ChatMessage.user_id == user.id,
                usable_chat_clause(),
                Task.queue_job_id < task.queue_job_id,
                Task.status.in_(["completed", "failed"]),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(12)
        ).all()
        history.reverse()
        project = (
            (db.get(Project, task.project_id) if task.project_id else None)
            if task.started_at
            else db.scalar(select(Project).where(Project.user_id == user.id, Project.is_current))
        )
        if task.started_at is None:
            task.project_id = project.id if project else None
        project_id = task.project_id
        source_ids = [message.id for message in batch]
        batch_times = [message.sent_at for message in batch]
        has_images = any(message.kind == "image" or message.image_urls for message in batch)
        memory_revision = user.memory_revision
        current_texts = [message.content for message in batch]
        snapshot = dict(task.input_snapshot or {})
        if "persona" not in snapshot:
            snapshot["persona"] = {
                "name": persona.name, "prompt": persona.prompt, "version": persona.version
            }
            task.input_snapshot = snapshot
        persona_snapshot = snapshot["persona"]
        instructions = (
            "你是该用户独立的文学创作伙伴 LISA。自然、具体地回应中文交流，允许只聊天。"
            "你及虚拟生活是虚构设定。不声称已完成尚未保存的作品或任务。"
            "用户消息可能延迟送达，以标注的消息时间理解旧消息，不假装刚刚发生。"
            "标注无法读取的非文本消息只代表用户发来了消息，不能推断图片或语音内容；请对方用文字说明。"
            f"当前 UTC 时间：{datetime.now(UTC).isoformat()}。\n"
            f"用户选定的性格名称：{persona_snapshot['name']}\n"
            f"性格设定：\n{persona_snapshot['prompt']}"
        )
        instructions += (
            MEMORY_INSTRUCTIONS
            + LIFE_INSTRUCTIONS
            + "\nlife_events的evidence逐字来自你本次reply。life_mentions只列本次回复明确提到的"
            "已有生活事件：memory_id用已提供的生活记忆id，evidence逐字来自reply；无则[]。"
        )
        base_instructions = instructions
        if project:
            instructions += creative_context(project, autonomy_enabled=settings.autonomy_enabled)
        instructions_without_optional = instructions
        share_ids, share_context = sharing_context(db, user.id)
        instructions += share_context
        instructions += interaction_context(db, user.id)
        instructions_without_memories = instructions
        memory_ids = []
        instructions += memory_context(
            db, user.id, project_id, purpose="chat", max_chars=1200, references=memory_ids
        )
        history_ids = [message.id for message in history]
        messages = [
            {
                "role": message.role,
                "content": (
                    f"[消息时间 {message.sent_at.isoformat()}]\n" + message_text(message)
                    if message.role == "user"
                    else message.content
                ),
            }
            for message in [*history, *batch]
        ]
        task.status = "running"
        task.started_at = task.started_at or datetime.now(UTC)
        user_id, qq_id = user.id, binding.qq_id
    error = None
    decision = None
    creative = bool(project_id)
    context_limited = False
    recalled = Context()
    try:
        query_texts = current_texts
        if has_images:
            visual_texts = await prepare_chat_images(
                settings, engine, task_id, user_id, memory_revision
            )
            query_texts = [visual_texts.get(source_id, text) for source_id, text in zip(
                source_ids, current_texts, strict=True
            )]
            for position, (sent_at, text) in enumerate(zip(batch_times, query_texts, strict=True)):
                messages[len(history) + position]["content"] = (
                    f"[消息时间 {sent_at.isoformat()}]\n" + text
                )
        recalled = await retrieve_context(
            settings, engine, task_id, user_id, project_id, "chat", "\n".join(query_texts)
        )
        with Session(engine) as db:
            check_memory_revision(db, user_id, memory_revision)
        if recalled.text:
            instructions = instructions_without_memories + recalled.text
            memory_ids = recalled.memory_ids
        try:
            messages = trim_history(
                settings,
                instructions,
                messages,
                len(history),
                schema=Dialogue if creative else Conversation,
            )
        except ModelInputTooLarge:
            # Keep persona/current input and work constraints before optional observations/memories.
            memory_ids = []
            recalled = Context()
            instructions = instructions_without_optional
            share_ids = set()
            try:
                messages = trim_history(
                    settings,
                    instructions,
                    messages,
                    len(history),
                    schema=Dialogue if creative else Conversation,
                )
            except ModelInputTooLarge:
                if not creative:
                    raise
                instructions = (
                    base_instructions
                    + "\n本轮作品资料超过对话预算，只聊天，不保存创作意见或委托写作；"
                    "可引导网页操作。"
                )
                messages = trim_history(
                    settings, instructions, messages, len(history), schema=Conversation
                )
                creative, context_limited = False, True
        used_history_ids = history_ids[len(history) + len(batch) - len(messages) :]
        with Session(engine) as db, db.begin():
            record_references(db, task_id, recalled)
        generator = generate or (generate_dialogue if creative else generate_conversation)
        result = await generator(settings, engine, task_id, user_id, instructions, messages)
        if creative:
            decision = Dialogue.model_validate(result)
            reply = decision.reply
        else:
            decision = (
                Conversation(reply=result)
                if isinstance(result, str)
                else Conversation.model_validate(result)
            )
            reply = decision.reply
    except ContextChanged:
        reply, error = (
            "记忆或聊天记录已更新，这次回复未采用。请重新发送消息继续。",
            "context_changed",
        )
    except ModelBusy:
        with Session(engine) as db, db.begin():
            task = db.get(Task, task_id)
            if task is None or task.status != "running":
                return
            if datetime.now(UTC) - task.created_at < timedelta(minutes=5):
                task.status = "pending"
                retry = True
            else:
                task.status, task.error = "failed", "model_busy"
                task.finished_at = datetime.now(UTC)
                clear_image_inputs(db, task_id)
                _outbound(db, task, qq_id, "对话服务暂时繁忙，请稍后再发消息。")
                retry = False
        if retry:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3})) from None
        return
    except BudgetExceeded:
        reply, error = "今日服务额度或本次对话额度已用尽，暂时无法生成回复。", "budget_exhausted"
    except ModelCallUnknown:
        reply, error = "这次对话请求的结果暂时无法确认，已停止自动重试。", "model_unknown"
    except ModelInputTooLarge:
        reply = "性格设定或本次消息过长，请缩短性格设定，或将消息分段发送。"
        error = "input_too_large"
    except (ModelCallFailed, ValueError):
        reply, error = "这次对话暂时未能完成，请稍后再试；消息较长时可以分段发送。", "model_failed"
    with Session(engine) as db, db.begin():
        user = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        clear_image_inputs(db, task_id)
        if user is None or not user.active or task is None or task.status != "running":
            if task and task.status == "running":
                task.status, task.error = "failed", "account_unavailable"
                task.finished_at = datetime.now(UTC)
            return
        if error is None and user.memory_revision != memory_revision:
            error = "context_changed"
            reply = "记忆或聊天记录已更新，这次回复未采用。请重新发送消息继续。"
        task.status, task.error = ("failed" if error else "completed"), error
        task.finished_at = datetime.now(UTC)
        if error is None:
            if context_limited:
                reply += "\n\n作品资料超过本轮对话的开发输入上限，创作修改或写作任务请在网页操作。"
            if isinstance(decision, Dialogue):
                record_creative_feedback(db, task, source_ids, decision.feedback_evidence)
                notice = apply_decision(db, user, project_id, current_texts, decision)
                if notice:
                    reply += "\n\n" + notice
                plan_after_chat(
                    db,
                    settings,
                    task,
                    decision.scene_plan if decision.commission is None else None,
                )
            if decision:
                if decision.share_chapter_id in share_ids:
                    link = chapter_link(db, settings, user_id, decision.share_chapter_id)
                    if link:
                        reply += "\n\n" + link
                record_chat_references(db, task_id, memory_ids, used_history_ids)
                adopt_candidates(db, user_id, project_id, decision.memories, source_ids)
            assistant_message = ChatMessage(
                user_id=user_id,
                task_id=task.id,
                role="assistant",
                content=reply,
                sent_at=datetime.now(UTC),
            )
            db.add(assistant_message)
            db.flush()
            if decision:
                life_ids = adopt_life_events(
                    db,
                    user_id,
                    decision.life_events,
                    reply,
                    source_message_id=assistant_message.id,
                )
                mentioned_ids = [
                    item.memory_id
                    for item in decision.life_mentions
                    if item.memory_id in memory_ids and item.evidence in reply
                ]
                queue_life_shares(db, user_id, task_id, life_ids + mentioned_ids)
        _outbound(db, task, qq_id, reply)
    logger.info("task=%s status=%s", task_id, "failed" if error else "completed")


def claim_outbound(
    db: Session, allow_qq: str | None, *, settings: Settings | None = None
) -> OutboundMessage | None:
    # Preserve each account's order without letting one delayed retry block other accounts.
    waiting = select(
        OutboundMessage.id,
        func.row_number()
        .over(partition_by=OutboundMessage.user_id, order_by=OutboundMessage.created_at)
        .label("position"),
    ).where(OutboundMessage.status.in_(["pending", "sending"]))
    if allow_qq is not None:
        waiting = waiting.where(OutboundMessage.qq_id == allow_qq)
    waiting = waiting.subquery()
    message = db.scalar(
        select(OutboundMessage)
        .where(
            OutboundMessage.id.in_(select(waiting.c.id).where(waiting.c.position == 1)),
            OutboundMessage.status == "pending",
            OutboundMessage.next_attempt_at <= datetime.now(UTC),
        )
        .order_by(OutboundMessage.created_at)
    )
    if message is None:
        return None
    user = db.scalar(select(Account).where(Account.id == message.user_id).with_for_update())
    if user is None:
        return None
    db.refresh(message, with_for_update=True)
    if message.status not in {"pending", "sending"}:
        return None
    if message.status == "sending":
        return None
    if message.next_attempt_at > datetime.now(UTC):
        return None
    binding = db.get(QQBinding, message.user_id)
    if not user.active or binding is None or binding.qq_id != message.qq_id:
        message.status = "cancelled"
        message.finished_at = datetime.now(UTC)
        return None
    task = db.get(Task, message.task_id)
    if task.kind == "contact":
        from lisa.contact_policy import contact_block_reason
        from lisa.progress import progress_is_current

        reason = (
            contact_block_reason(db, settings, user.id, task=task)
            if settings is not None and task.status == "completed"
            else "unavailable"
        )
        if not reason and not progress_is_current(
            db, user.id, (task.input_snapshot or {}).get("progress")
        ):
            reason = "progress_changed"
        if reason:
            message.status, message.finished_at = "cancelled", datetime.now(UTC)
            task.status, task.error = "cancelled", reason
            return None
    message.status = "sending"
    message.attempts += 1
    db.flush()
    return message


def finish_delivery(db: Session, message_id: UUID, status: str, gateway_id: str | None = None):
    message = db.get(OutboundMessage, message_id)
    if message is None:
        return
    user = db.scalar(select(Account).where(Account.id == message.user_id).with_for_update())
    if user is None:
        return
    db.refresh(message, with_for_update=True)
    if message.status != "sending":
        return
    if status == "failed" and message.attempts < 3:
        message.status = "pending"
        message.next_attempt_at = datetime.now(UTC) + timedelta(seconds=5)
    else:
        message.status, message.message_id = status, gateway_id
        message.finished_at = datetime.now(UTC)
        if status == "sent":
            if db.get(Task, message.task_id).kind == "contact":
                from lisa.proactive import adopt_delivered_contact

                adopt_delivered_contact(db, message)
            mark_life_shared(db, message.task_id, message.finished_at)
