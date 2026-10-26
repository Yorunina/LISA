import logging
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from procrastinate.exceptions import JobRetry
from procrastinate.retry import RetryDecision
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from lisa.chat_style import expression_guide
from lisa.dialogue import (
    MEMORY_INSTRUCTIONS,
    Conversation,
    Dialogue,
    apply_decision,
    chat_project_context,
    generate_conversation,
    generate_dialogue,
)
from lisa.life import LIFE_INSTRUCTIONS, adopt_life_events, mark_life_shared, queue_life_shares
from lisa.life_plans import apply_explicit_plan_text, enqueue_agent_plan, ensure_current_plan
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
from lisa.queue import CHAT_SILENCE_SECONDS, enqueue_chat, reschedule_chat
from lisa.recall import ContextChanged, check_memory_revision, retrieve_context
from lisa.retrieval import Context, record_references
from lisa.scheduling import discard_pending_plans, plan_after_chat, record_creative_feedback
from lisa.settings import Settings
from lisa.sharing import chapter_link, sharing_context
from lisa.style_profiles import resolve_personality_style
from lisa.style_runtime import chat_scene
from lisa.vision import (
    MAX_IMAGES,
    clear_image_inputs,
    image_url_allowed,
    message_text,
    prepare_chat_images,
)

logger = logging.getLogger(__name__)

# The model chooses the rhythm, while these bounds keep malformed output from
# turning one logical turn into a long queue.
MIN_MESSAGE_INTERVAL_SECONDS = 0.5
MAX_MESSAGE_INTERVAL_SECONDS = 8.0
DEFAULT_MESSAGE_INTERVAL_SECONDS = 1.0
MAX_MESSAGE_TOTAL_WAIT_SECONDS = 30.0


def _chat_cutoff(now: datetime) -> datetime:
    return now + timedelta(seconds=CHAT_SILENCE_SECONDS)


def _message_parts(decision, fallback: str) -> list[str]:
    parts = getattr(decision, "messages", None) if decision is not None else None
    if not parts:
        return [fallback]
    cleaned = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return cleaned or [fallback]


def _message_intervals(decision, part_count: int) -> list[float]:
    """Normalize model-selected waits to one bounded value per part."""
    raw = getattr(decision, "message_intervals", None) if decision is not None else None
    return _normalize_message_intervals(raw, part_count)


def _normalize_message_intervals(raw, part_count: int) -> list[float]:
    values = raw if isinstance(raw, list) else []
    normalized = [0.0]
    total = 0.0
    for index in range(1, part_count):
        value = values[index] if index < len(values) else DEFAULT_MESSAGE_INTERVAL_SECONDS
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = DEFAULT_MESSAGE_INTERVAL_SECONDS
        if not math.isfinite(value):
            value = DEFAULT_MESSAGE_INTERVAL_SECONDS
        value = min(MAX_MESSAGE_INTERVAL_SECONDS, max(MIN_MESSAGE_INTERVAL_SECONDS, value))
        remaining = MAX_MESSAGE_TOTAL_WAIT_SECONDS - total
        value = min(value, max(0.0, remaining))
        normalized.append(round(value, 3))
        total += value
    return normalized


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
    # Keep only the gateway event identity needed for deduplication; the
    # message body itself is transient and is removed after task completion.
    db.add(
        ServiceEvent(
            id=key,
            bot_id=bot_id,
            message_id=message_id,
            qq_id=qq_id,
            sent_at=sent_at,
            status="chat",
        )
    )
    discard_pending_plans(db, user.id)
    # A new user turn interrupts ordinary chat parts that have not reached QQ.
    # In-flight transport calls finish with their real gateway result; they are
    # never blindly replayed or folded into the new turn.
    interrupted_tasks = set(db.scalars(
        update(OutboundMessage)
        .where(
            OutboundMessage.user_id == user.id,
            OutboundMessage.status == "pending",
            OutboundMessage.task_id.in_(
                select(Task.id).where(Task.user_id == user.id, Task.kind == "chat")
            ),
        )
        .values(status="cancelled", content="", finished_at=now)
        .returning(OutboundMessage.task_id)
    ))
    _clear_terminal_outbound(db, interrupted_tasks)
    user.last_active_at = max(user.last_active_at, sent_at) if user.last_active_at else sent_at
    received_at = datetime.now(UTC)
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
        task.request = {"chat_silence_until": _chat_cutoff(received_at).isoformat()}
        db.add(task)
        db.flush()
        task.queue_job_id = enqueue_chat(db, task.id, user.id)
    else:
        request = dict(task.request or {})
        cutoff = _chat_cutoff(received_at)
        request["chat_silence_until"] = cutoff.isoformat()
        task.request = request
        if task.queue_job_id is not None:
            reschedule_chat(db, task.queue_job_id, until=cutoff)
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


def _clear_terminal_outbound(db: Session, task_ids) -> None:
    """Cancellation and delivery share one terminal reply-body cleanup rule."""
    if not task_ids:
        return
    active = aliased(OutboundMessage)
    db.execute(
        update(OutboundMessage).where(
            OutboundMessage.task_id.in_(task_ids),
            ~exists(select(1).where(
                active.task_id == OutboundMessage.task_id,
                active.status.in_(["pending", "sending"]),
            )),
        ).values(content="")
    )


def _chat_interrupted(db: Session, task: Task) -> bool:
    """New input can arrive before this turn has any outbox rows to cancel."""
    return bool(
        task.kind == "chat"
        and task.queue_job_id is not None
        and db.scalar(
            select(Task.id).where(
                Task.user_id == task.user_id,
                Task.kind == "chat",
                Task.queue_job_id > task.queue_job_id,
            ).limit(1)
        )
    )


def _outbound(
    db: Session,
    task: Task,
    qq_id: str,
    content: str,
    meme=None,
    *,
    parts: list[str] | None = None,
    chat_message_ids: list[UUID] | None = None,
    intervals: list[float] | None = None,
) -> None:
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.task_id == task.id)):
        return
    values = parts or [content]
    if intervals is None:
        intervals = [0.0] + [DEFAULT_MESSAGE_INTERVAL_SECONDS] * (len(values) - 1)
    else:
        # Apply the same policy to callers other than the chat model path.
        intervals = _normalize_message_intervals(intervals, len(values))
    created = datetime.now(UTC)
    interrupted = _chat_interrupted(db, task)
    task.result = {**(task.result or {}), "outbound_count": len(values)}
    for sequence, value in enumerate(values):
        db.add(
            OutboundMessage(
                user_id=task.user_id,
                task_id=task.id,
                sequence=sequence,
                chat_message_id=(chat_message_ids or [None] * len(values))[sequence],
                qq_id=qq_id,
                content="" if interrupted else value,
                interval_seconds=intervals[sequence],
                meme_id=meme.id if meme and sequence == 0 else None,
                meme_revision=meme.revision if meme and sequence == 0 else None,
                status="cancelled" if interrupted else "pending",
                finished_at=created if interrupted else None,
                # Only the first part is immediately eligible. Each later
                # part is released by finish_delivery after the prior part is
                # confirmed sent, so slow transports never catch up in a burst.
                next_attempt_at=created if sequence == 0 else created + timedelta(days=3650),
            )
        )


def _chat_instructions(db, user_id, task, persona, current_texts) -> str:
    """Freeze the selected persona/style for this attempt without retaining raw history."""
    snapshot = dict(task.input_snapshot or {})
    if "persona" not in snapshot:
        snapshot["persona"] = {
            "name": persona.name, "prompt": persona.prompt, "version": persona.version
        }
        task.input_snapshot = snapshot
    persona_snapshot = snapshot["persona"]
    if "style_card" not in snapshot:
        scene = chat_scene("\n".join(current_texts))
        snapshot["style_card"] = {
            "scene": scene,
            **resolve_personality_style(
                db,
                user_id,
                scene=scene,
                fallback=persona_snapshot["prompt"],
            ),
        }
        task.input_snapshot = snapshot
    style_card = snapshot["style_card"]
    instructions = (
        "你是该用户独立的文学创作伙伴 LISA。自然、具体地回应中文交流，允许只聊天。"
        "reply是本轮完整回复；messages是按语义顺序发送的分条列表，通常一到三条，允许一条。"
        "不要机械切碎或新增reply之外的事实，控制本轮总量，先接住用户重点。"
        "message_intervals与messages等长，首项填0，后续各项填上一条确认发送成功后等待的秒数，"
        "按自然节奏自行选择；系统会限制每项0.5至8秒及整轮等待30秒。"
        "你及虚拟生活是虚构设定。不声称已完成尚未保存的作品或任务。"
        "用户消息可能延迟送达，以标注的消息时间理解旧消息，不假装刚刚发生。"
        "标注无法读取的非文本消息只代表用户发来了消息，不能推断图片或语音内容；请对方用文字说明。"
        f"当前 UTC 时间：{datetime.now(UTC).isoformat()}。\n"
        f"用户选定的性格名称：{persona_snapshot['name']}\n"
        f"本轮交流风格卡（按当前场景取用）：\n{style_card['prompt']}"
    )
    instructions += (
        "\n" + expression_guide() + MEMORY_INSTRUCTIONS
        + LIFE_INSTRUCTIONS
        + "\nlife_events的evidence逐字来自你本次reply。life_mentions只列本次回复明确提到的"
        "已有生活事件：memory_id用已提供的生活记忆id，evidence逐字来自reply；无则[]。"
    )
    instructions += (
        "\nmeme_id只选本轮提供的共有表情素材id或null；没有候选时必须null。"
        "用户明确要求少发、停用或恢复表情时，meme_preference分别填reduced/off/normal，"
        "meme_preference_evidence逐字引用本批用户该要求；普通交流均为null和空字符串。"
    )
    return instructions


def _adopt_chat_decision(
    db, settings, user, task, decision, reply, reply_parts, *,
    project_id, source_ids, current_texts, memory_ids, share_ids, context_limited,
) -> str:
    """Apply a successful turn in the caller's transaction before clearing its inbox."""
    user_id, task_id = user.id, task.id
    ensure_current_plan(db, user.id)
    if context_limited:
        reply += "\n\n本轮聊天上下文超过应用输入预算，创作修改或写作任务请在网页操作。"
        reply_parts[-1] += (
            "\n\n本轮聊天上下文超过应用输入预算，创作修改或写作任务请在网页操作。"
        )
    if isinstance(decision, Dialogue):
        record_creative_feedback(db, task, source_ids, decision.feedback_evidence)
        notice = apply_decision(db, user, project_id, current_texts, decision)
        if notice:
            reply += "\n\n" + notice
            reply_parts[-1] += "\n\n" + notice
        plan_after_chat(
            db,
            settings,
            task,
            decision.scene_plan if decision.commission is None else None,
        )
    if decision:
        apply_explicit_plan_text(db, user.id, source_ids, current_texts)
        if settings.model_api_key.get_secret_value():
            enqueue_agent_plan(db, user.id)
        from lisa.models import SharedPreference
        evidence = decision.meme_preference_evidence
        if decision.meme_preference and evidence and any(
            evidence in text for text in current_texts
        ) and any(word in evidence for word in ("表情", "发图")):
            preference = db.get(SharedPreference, user_id)
            if preference:
                preference.meme_mode = decision.meme_preference
            else:
                db.add(SharedPreference(
                    user_id=user_id, meme_mode=decision.meme_preference
                ))
            db.flush()
        if decision.share_chapter_id in share_ids:
            link = chapter_link(db, settings, user_id, decision.share_chapter_id)
            if link:
                reply += "\n\n" + link
                reply_parts[-1] += "\n\n" + link
        record_chat_references(db, task_id, memory_ids)
        adopt_candidates(
            db, user_id, project_id, decision.memories, source_ids, source_task_id=task_id
        )
    if decision:
        life_ids = adopt_life_events(
            db,
            user_id,
            decision.life_events,
            reply,
            source_task_id=task.id,
        )
        mentioned_ids = [
            item.memory_id
            for item in decision.life_mentions
            if item.memory_id in memory_ids and item.evidence in reply
        ]
        queue_life_shares(db, user_id, task_id, life_ids + mentioned_ids)
    return reply


async def execute_chat(settings: Settings, engine, task_id: str, *, generate=None) -> None:
    task_id = UUID(str(task_id))
    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        if task is None or task.status != "pending":
            if task is not None and task.status in {"cancelled", "failed", "completed"}:
                db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
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
            db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
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
            db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
            return
        # Raw chat history is intentionally not retained or replayed.  The
        # current batch is the only message context supplied to the model;
        # durable continuity comes from explicit memories and saved project data.
        history = []
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
        instructions = _chat_instructions(db, user.id, task, persona, current_texts)
        base_instructions = instructions
        if project:
            instructions += chat_project_context(
                project, autonomy_enabled=settings.autonomy_enabled
            )
        instructions_without_optional = instructions
        share_ids, share_context = sharing_context(db, user.id)
        instructions += share_context
        instructions_without_memories = instructions
        memory_ids = []
        instructions += memory_context(
            db, user.id, project_id, purpose="chat", max_chars=1200, references=memory_ids
        )
        from lisa.shared_retrieval import sent_meme_context
        messages = [
            {
                "role": message.role,
                "content": (
                    f"[消息时间 {message.sent_at.isoformat()}]\n" + message_text(message)
                    if message.role == "user"
                    else message.content + sent_meme_context(db, message)
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
            from lisa.shared_retrieval import shared_references_current
            if not shared_references_current(db, recalled, user_id):
                raise ContextChanged
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
                    + "\n本轮聊天上下文超过应用预算，只聊天，不保存创作意见或委托写作；"
                    "可引导网页操作。"
                )
                messages = trim_history(
                    settings,
                    instructions,
                    messages,
                    len(history),
                    schema=Conversation,
                )
                creative, context_limited = False, True
        with Session(engine) as db, db.begin():
            record_references(db, task_id, recalled)
        generator = generate or (generate_dialogue if creative else generate_conversation)
        result = await generator(settings, engine, task_id, user_id, instructions, messages)
        if creative:
            decision = result if isinstance(result, Dialogue) else Dialogue.model_validate(result)
            reply = decision.reply
        else:
            decision = (
                Conversation(reply=result)
                if isinstance(result, str)
                else Conversation.model_validate(result)
            )
            reply = decision.reply
        reply_parts = _message_parts(decision, reply)
        reply_intervals = _message_intervals(decision, len(reply_parts))
    except ContextChanged:
        reply, error = (
            "记忆已更新，这次回复未采用。请重新发送消息继续。",
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
                db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
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
            if task and task.status in {"cancelled", "failed", "completed"}:
                db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
            return
        from lisa.shared_retrieval import select_meme, shared_references_current
        if error is None and (
            user.memory_revision != memory_revision
            or not shared_references_current(db, recalled, user_id)
        ):
            error = "context_changed"
            reply = "记忆已更新，这次回复未采用。请重新发送消息继续。"
            reply_parts = [reply]
        task.status, task.error = ("failed" if error else "completed"), error
        task.finished_at = datetime.now(UTC)
        if error is None:
            reply = _adopt_chat_decision(
                db, settings, user, task, decision, reply, reply_parts,
                project_id=project_id, source_ids=source_ids, current_texts=current_texts,
                memory_ids=memory_ids, share_ids=share_ids, context_limited=context_limited,
            )
        meme = (
            select_meme(db, user_id, decision.meme_id, recalled) if decision and not error else None
        )
        outbound_parts = reply_parts if error is None else [reply]
        outbound_intervals = reply_intervals if error is None else [0.0]
        _outbound(
            db,
            task,
            qq_id,
            reply,
            meme,
            parts=outbound_parts,
            intervals=outbound_intervals,
            chat_message_ids=None,
        )
        # The rows above are only a transactional inbox for debounce, retries,
        # and image processing.  Once this task has produced its result, remove
        # every raw user input so LISA cannot maintain a parallel chat archive.
        db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
    logger.info("task=%s status=%s", task_id, "failed" if error else "completed")


def claim_outbound(
    db: Session, allow_qq: str | None, *, settings: Settings | None = None
) -> OutboundMessage | None:
    # Preserve each account's order without letting one delayed retry block other accounts.
    prior = aliased(OutboundMessage)
    waiting = select(
        OutboundMessage.id,
        func.row_number()
        .over(
            partition_by=OutboundMessage.user_id,
            order_by=(OutboundMessage.created_at, OutboundMessage.sequence),
        )
        .label("position"),
    ).where(
        OutboundMessage.status.in_(["pending", "sending"]),
        ~exists(
            select(1).where(
                prior.task_id == OutboundMessage.task_id,
                prior.sequence < OutboundMessage.sequence,
                prior.status.not_in(["sent", "cancelled"]),
            )
        ),
    )
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
        .order_by(OutboundMessage.created_at, OutboundMessage.sequence)
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
        message.status, message.content = "cancelled", ""
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
            message.status, message.content = "cancelled", ""
            message.finished_at = datetime.now(UTC)
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
    task = db.get(Task, message.task_id)
    if status == "failed" and message.attempts < 3 and not _chat_interrupted(db, task):
        message.status = "pending"
        message.next_attempt_at = datetime.now(UTC) + timedelta(seconds=5)
    else:
        message.status, message.message_id = status, gateway_id
        message.finished_at = datetime.now(UTC)
        if status in {"failed", "unknown", "cancelled"}:
            db.execute(
                update(OutboundMessage)
                .where(
                    OutboundMessage.task_id == message.task_id,
                    OutboundMessage.sequence > message.sequence,
                    OutboundMessage.status == "pending",
                )
                .values(status="cancelled", content="", finished_at=message.finished_at)
            )
        if status == "sent":
            next_message = db.scalar(
                select(OutboundMessage)
                .where(
                    OutboundMessage.task_id == message.task_id,
                    OutboundMessage.sequence == message.sequence + 1,
                    OutboundMessage.status == "pending",
                )
                .with_for_update()
            )
            if next_message is not None:
                next_message.next_attempt_at = message.finished_at + timedelta(
                    seconds=float(next_message.interval_seconds or DEFAULT_MESSAGE_INTERVAL_SECONDS)
                )
            if message.chat_message_id:
                db.execute(
                    update(ChatMessage)
                    .where(ChatMessage.id == message.chat_message_id)
                    .values(context_excluded=False)
                )
            if task.kind == "contact":
                from lisa.proactive import adopt_delivered_contact

                adopt_delivered_contact(db, message)
                mark_life_shared(db, message.task_id, message.finished_at)
            elif not db.scalar(
                select(OutboundMessage.id).where(
                    OutboundMessage.task_id == message.task_id,
                    OutboundMessage.status != "sent",
                )
            ):
                # A multi-part chat turn is shared only after every part is
                # confirmed, so a cancelled or unknown tail cannot be claimed
                # as already shown to the user.
                mark_life_shared(db, message.task_id, message.finished_at)
        # Keep delivery status for retry/order diagnostics, but remove message
        # bodies once the logical turn is terminal.
        _clear_terminal_outbound(db, [message.task_id])
