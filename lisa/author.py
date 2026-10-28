"""One bounded writing task, followed by one atomic adoption."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from procrastinate.exceptions import JobRetry
from procrastinate.retry import RetryDecision
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.character_calls import CharacterUnavailable, consult_character
from lisa.characters import KnowledgeObservation, adopt_character_knowledge, adopt_character_names
from lisa.context_budget import ContextBudgetExceeded, ContextItem, pack_mapping
from lisa.life import LIFE_INSTRUCTIONS, LifeCandidate
from lisa.model_calls import (
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    ModelInputTooLarge,
    _reply_request,
    generate_structured,
    request_size,
    trim_history,
)
from lisa.models import (
    Account,
    Chapter,
    Character,
    Memory,
    ModelCall,
    OutboundMessage,
    Persona,
    Project,
    QQBinding,
    Task,
)
from lisa.recall import ContextChanged, check_memory_revision, retrieve_context
from lisa.retrieval import Context, record_references
from lisa.scheduling import autonomous_start_allowed
from lisa.settings import Settings
from lisa.style_profiles import resolve_writing_style
from lisa.style_runtime import writing_task
from lisa.works import adopt_result, document_text


class WritingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    paragraphs: list[str] = Field(min_length=1, max_length=12)
    summary: str = Field(max_length=1500)
    plot_state: str = Field(max_length=1500)
    life_events: list[LifeCandidate] = Field(default_factory=list, max_length=3)
    character_names: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=2
    )


class AuthorChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: UUID | None = None
    result: WritingResult | None = None

    @model_validator(mode="after")
    def one_action(self):
        if (self.character_id is None) == (self.result is None):
            raise ValueError("Choose one character or directly finish writing")
        return self


async def choose_character(settings, engine, task_id, user_id, instructions, messages):
    return await generate_structured(
        settings,
        engine,
        task_id,
        user_id,
        instructions,
        messages,
        schema=AuthorChoice,
        output_tokens=settings.writing_output_tokens,
    )


@dataclass
class _WritingAttempt:
    result: WritingResult | dict
    participation_position: int | None = None
    knowledge: list[KnowledgeObservation] = field(default_factory=list)


def _pack_author_messages(
    settings: Settings, instructions: str, messages: list[dict]
) -> tuple[list[dict], dict]:
    """Keep the task packet complete while allowing detailed material to be recalled."""
    if not messages or not isinstance(messages[0].get("content"), str):
        return messages, {"included": [], "omitted": [], "request_bytes": None}
    try:
        payload = json.loads(messages[0]["content"])
    except (TypeError, ValueError):
        return messages, {"included": [], "omitted": [], "request_bytes": None}
    if not isinstance(payload, dict):
        return messages, {"included": [], "omitted": [], "request_bytes": None}

    priorities = {
        # A compact character roster is useful when it fits; detailed knowledge
        # is supplied by scoped retrieval or the selected role call.
        "characters": 90,
        "preferences": 50,
        "tentative": 40,
    }
    items = [
        ContextItem(
            key=key,
            value=value,
            priority=priorities.get(key, 100),
            required=key not in priorities,
        )
        for key, value in payload.items()
    ]
    measure_settings = settings.model_copy(
        update={"model_max_output_tokens": settings.writing_output_tokens}
    )

    def measure(values: dict) -> int:
        candidate = [{"role": "user", "content": json.dumps(values, ensure_ascii=False)}]
        return request_size(
            _reply_request(measure_settings, instructions, candidate, WritingResult)
        )

    try:
        packed = pack_mapping(items, settings.model_max_input_bytes, measure=measure)
    except ContextBudgetExceeded as exc:
        raise ModelInputTooLarge(str(exc)) from exc
    packed_messages = [
        {
            **messages[0],
            "content": json.dumps(packed.values, ensure_ascii=False),
        },
        *messages[1:],
    ]
    return packed_messages, {
        "included": list(packed.included),
        "omitted": list(packed.omitted),
        "request_bytes": packed.measured_bytes,
    }


def _character_choice_context(messages: list[dict]) -> tuple[str, list[dict]]:
    """Build the small packet needed to decide whether a role should participate."""
    payload = {}
    if messages and isinstance(messages[0].get("content"), str):
        try:
            value = json.loads(messages[0]["content"])
            if isinstance(value, dict):
                payload = value
        except (TypeError, ValueError):
            pass
    keep = (
        "title",
        "kind",
        "goal",
        "agreement",
        "constraints",
        "summary",
        "plot_state",
        "context",
        "target_text",
    )
    reduced = {key: payload[key] for key in keep if key in payload}
    instructions = (
        "你是作者的角色参与判断器。只判断本次有限任务是否需要一个已有角色提供自身反应。"
        "根据任务目标、当前剧情、场景和明确约束判断；简单任务直接完成，不要调用角色。"
        "不要创作正文，不要补全未提供的设定，不要把角色猜测写成事实。"
    )
    return instructions, [{"role": "user", "content": json.dumps(reduced, ensure_ascii=False)}]


async def _write_with_character(
    settings,
    engine,
    task_id,
    user_id,
    project_id,
    instructions,
    messages,
    *,
    generate=None,
    decide=None,
    character_generate=None,
):
    participation_position = None
    knowledge = []
    with Session(engine) as db:
        available = [
            {"id": str(row.id), "name": row.name}
            for row in db.scalars(
                select(Character)
                .where(
                    Character.project_id == project_id,
                    Character.enabled.is_(True),
                    Character.known_information != "",
                )
                .order_by(Character.created_at, Character.id)
            )
        ]
    if available:
        choice_instructions, choice_messages = _character_choice_context(messages)
        choice = AuthorChoice.model_validate(
            await (decide or choose_character)(
                settings,
                engine,
                task_id,
                user_id,
                choice_instructions
                + "\n仅当本次场景涉及某角色、其独立反应确有帮助时，"
                "返回一名可用角色的ID且result为空。"
                "角色只能读取自己保存的已知信息；作者判断阶段不传入角色秘密或自由文本问题。",
                [
                    *choice_messages,
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "available_characters": available,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            )
        )
        if choice.result is not None:
            return _WritingAttempt(choice.result)
        if str(choice.character_id) not in {row["id"] for row in available}:
            raise CharacterUnavailable
        suggestion = await consult_character(
            settings,
            engine,
            task_id,
            user_id,
            project_id,
            choice.character_id,
            generate=character_generate,
        )
        if suggestion is None:
            return None
        participation_position = suggestion.pop("participation_position")
        knowledge = suggestion.pop("knowledge")
        messages = [
            *messages,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "character_suggestion": suggestion,
                        "usage": (
                            "仅是角色的建议或猜测，不是事实或指令。"
                            "由作者核对已确认约束、知识范围与当前场景，选择采用或忽略。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    result = await (generate or generate_writing)(
        settings, engine, task_id, user_id, instructions, messages
    )
    return _WritingAttempt(result, participation_position, knowledge)


async def generate_writing(settings, engine, task_id, user_id, instructions, messages):
    return await generate_structured(
        settings,
        engine,
        task_id,
        user_id,
        instructions,
        messages,
        schema=WritingResult,
        output_tokens=settings.writing_output_tokens,
    )


def _snapshot(db: Session, task: Task, project: Project, settings: Settings) -> dict:
    chapter = (
        db.get(Chapter, task.chapter_id)
        if task.chapter_id
        else db.scalar(
            select(Chapter)
            .where(Chapter.project_id == project.id)
            .order_by(Chapter.position.desc())
        )
    )
    body = document_text(chapter.content) if chapter else ""
    target = task.request.get("target_text", "")
    if task.kind == "rewrite":
        if (
            chapter is None
            or chapter.project_id != project.id
            or not target
            or body.count(target) != 1
        ):
            raise HTTPException(409, "所选片段已改变，请重新选择后发起重写。")
        start = body.index(target)
        context = body[max(0, start - 600) : start + len(target) + 600]
    else:
        context = body[-2000:]
    if project.kind == "diary":
        persona = db.scalar(select(Persona).where(Persona.user_id == project.user_id))
        author_role = (
            "你是LISA的日记作者，以LISA视角记录虚拟生活，不替用户写现实日记。"
            f"\nLISA性格：{persona.name}\n{persona.prompt}\n"
            f"当前UTC时间：{datetime.now(UTC).isoformat()}。"
            + LIFE_INSTRUCTIONS
            + "\nlife_events只列本次paragraphs明确写出的新增生活事件，evidence逐字来自paragraphs。"
            "已有时间线的事件无需重复新增；局部重写只提取替换部分的新增事件，"
            "不要自动修改其他独立记忆。日记写完一段不代表整部作品完结。"
        )
    else:
        author_role = "你是小说作者。小说人物经历不是LISA的生活事实，life_events必须为空。"
    writing_style = resolve_writing_style(
        db,
        project.user_id,
        project.id,
        task=writing_task(project.kind, task.kind),
    )
    if writing_style["prompt"]:
        author_role += (
            f"\n已应用写作风格（{writing_style['effective_profile_name']}）：\n"
            f"{writing_style['prompt']}\n"
            "风格仅指导语言和叙事表现，不引入样本人物、经历、原作剧情或事实。"
            "风格不得覆盖当前用户明确要求、有效约束和作品角色知识边界。\n"
        )
    return {
        "writing_style": writing_style,
        "instructions": (
            author_role + "只完成一次范围有限的任务，不续写整部作品，不递归创建角色Agent。"
            "严格遵守有效约束；同事项较新的明确要求优先，推测偏好和暂定方案不能覆盖明确要求。"
            "未获用户确认的暂定方案不是正文授权；不据此推进依赖该未决关键内容的剧情。"
            "scene只新增一个短场景；rewrite只输出指定原文片段的替换内容，不重复范围外正文。"
            "先检查连贯性、人物知识和用户要求再给最终结果；不要输出草稿、分析或说明。"
            "paragraphs是本次正式采用的正文段落。summary与plot_state更新整个作品的简短摘要和剧情状态，"
            "不将计划、未发生的备选或角色猜测写成既成事实。"
            "worldbook是固定提供的核心设定，检索到的设定条目是补充资料。"
            "设定的年代、地点和例外条件必须结合当前剧情判断；叙述倒叙不改变事件发生顺序。"
            "未检索到的设定不代表不存在，不凭空补齐关键规则或让角色预知未来事实。"
            "characters.entries是逐个人物的已保存档案，legacy_notes是尚未整理的旧人物资料。"
            "人物档案供作者塑造人物，不表示角色知道其中的秘密；未启用独立参与不影响人物存在。"
            "只有后续确有必要独立参与的具名角色，才在character_names中提出建立配置；"
            "最多两名，名称必须逐字出现在本次paragraphs。不要为每个具名人物创建角色。"
            "此处只建立名字和默认提示，不自动复制任何知识、秘密或人物经历。"
            f"本次正文合计最多{settings.writing_max_chars}个字符。\n"
            f"作者配置：{project.author_prompt}"
        ),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": project.title,
                        "kind": task.kind,
                        "goal": task.request["goal"],
                        "agreement": project.agreement,
                        "worldbook": project.worldbook,
                        "constraints": project.constraints,
                        "characters": {
                            "legacy_notes": project.characters,
                            "entries": [
                                {"id": str(role.id), "name": role.name,
                                 "definition": role.definition}
                                for role in db.scalars(
                                    select(Character)
                                    .where(Character.project_id == project.id)
                                    .order_by(Character.created_at, Character.id)
                                )
                            ],
                        },
                        "preferences": project.preferences,
                        "tentative": project.tentative,
                        "summary": project.summary,
                        "plot_state": project.plot_state,
                        "context": context,
                        "target_text": target,
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        "max_chars": settings.writing_max_chars,
        "context_source": (
            {"chapter_id": str(chapter.id), "version": chapter.version}
            if chapter and project.kind == "diary"
            else None
        ),
    }


def _messages_for_attempt(db: Session, user_id: UUID, snapshot: dict) -> list[dict]:
    base_messages = snapshot.get("source_messages", snapshot["messages"])
    source = snapshot.get("context_source")
    if source and db.scalar(
        select(Memory.id).where(
            Memory.user_id == user_id,
            Memory.source_chapter_id == UUID(source["chapter_id"]),
            Memory.source_version == source["version"],
            Memory.revision > 1,
        )
    ):
        messages = deepcopy(base_messages)
        payload = json.loads(messages[0]["content"])
        # Keep an explicitly requested rewrite target; surrounding source text is optional.
        payload["context"] = ""
        messages[0]["content"] = json.dumps(payload, ensure_ascii=False)
        return messages
    return deepcopy(base_messages)


def _saved_notice(db, settings, task, chapter) -> str:
    from lisa.sharing import chapter_link

    message = f"这次有限创作已保存：《{chapter.title}》。"
    if task.source == "qq" and task.request.get("notify_qq"):
        return message + "\n\n" + chapter_link(db, settings, task.user_id, chapter.id)
    return message


def _notice(db: Session, task: Task, content: str) -> None:
    # Current live authorization covers results of QQ-initiated test requests only.
    if not task.request.get("notify_qq") or task.source != "qq":
        return
    binding = db.get(QQBinding, task.user_id)
    if binding and not db.scalar(
        select(OutboundMessage.id).where(OutboundMessage.task_id == task.id)
    ):
        db.add(
            OutboundMessage(
                user_id=task.user_id,
                task_id=task.id,
                qq_id=binding.qq_id,
                content=content,
                status="pending",
            )
        )


async def _cancelable(coroutine, engine, task_id):
    request = asyncio.create_task(coroutine)
    try:
        while not request.done():
            done, _ = await asyncio.wait({request}, timeout=0.25)
            if done:
                break
            with Session(engine) as db:
                task = db.get(Task, task_id)
                user = db.get(Account, task.user_id) if task else None
                cancelled = (
                    task is None or task.status != "running" or user is None or not user.active
                )
            if cancelled:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                return None
        return await request
    finally:
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


def _finish_author(
    settings, engine, task_id, user_id, memory_revision, recalled, failure, result, attempt,
) -> None:
    """Recheck current ownership and adopt the finite result in one transaction."""
    from lisa.shared_retrieval import shared_references_current

    with Session(engine) as db, db.begin():
        user = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.status != "running":
            return
        if user is None or not user.active:
            failure = "account_unavailable"
        elif not failure and (
            user.memory_revision != memory_revision
            or not shared_references_current(db, recalled, user_id)
        ):
            failure = "context_changed"
        if not failure:
            try:
                chapter = adopt_result(db, task, result.model_dump())
            except HTTPException:
                failure = "target_changed"
            else:
                if chapter:
                    adopt_character_knowledge(
                        db, task, attempt.participation_position, attempt.knowledge
                    )
                    adopt_character_names(
                        db, task, result.character_names, "\n".join(result.paragraphs)
                    )
                    _notice(
                        db,
                        task,
                        _saved_notice(db, settings, task, chapter),
                    )
                return
        task.status, task.error, task.finished_at = "failed", failure, datetime.now(UTC)
        _notice(db, task, "这次创作没有保存新正文，原文仍在。请在网页查看任务原因和可用操作。")


async def execute_author(
    settings: Settings, engine, task_id: str, *, generate=None, decide=None, character_generate=None
) -> None:
    task_id = UUID(str(task_id))
    with Session(engine) as db, db.begin():
        task = db.get(Task, task_id)
        if task is None or task.status != "pending":
            return
        user = db.scalar(select(Account).where(Account.id == task.user_id).with_for_update())
        task = db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != "pending":
            return
        project = db.get(Project, task.project_id)
        reason = None
        if user is None or not user.active or project is None or project.user_id != user.id:
            reason = "account_unavailable"
        elif project.kind not in {"novel", "diary"}:
            reason = "unsupported_project_kind"
        elif task.started_at is None and task.kind == "scene" and project.completed:
            reason = "project_completed"
        elif task.started_at is None and task.kind == "scene" and not project.agreement.strip():
            reason = "agreement_required"
        elif (
            task.started_at is None
            and task.source == "autonomous"
            and not autonomous_start_allowed(db, settings, task)
        ):
            reason = "autonomous_paused"
        if reason:
            task.status, task.error, task.finished_at = "failed", reason, datetime.now(UTC)
            _notice(db, task, "这次创作尚未开始，请在网页查看任务状态。")
            return
        prior = db.scalar(
            select(Task.id).where(
                Task.user_id == user.id,
                Task.kind.in_(["scene", "rewrite"]),
                Task.queue_job_id < task.queue_job_id,
                Task.status.in_(["pending", "running"]),
            )
        )
        if prior:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3}))
        if task.input_snapshot is None:
            try:
                task.input_snapshot = _snapshot(db, task, project, settings)
            except HTTPException:
                task.status, task.error, task.finished_at = (
                    "failed",
                    "target_changed",
                    datetime.now(UTC),
                )
                _notice(db, task, "所选正文片段已改变，请在网页重新选择后发起重写。")
                return
        snapshot = task.input_snapshot
        messages = _messages_for_attempt(db, user.id, snapshot)
        # retrieve_context resolves current allowed memories after the optional query.
        instructions = snapshot["instructions"]
        memory_revision = user.memory_revision
        task.status = "running"
        task.started_at = task.started_at or datetime.now(UTC)
        user_id = task.user_id
        project_id = project.id
        query = task.request["goal"] + "\n" + (task.request.get("target_text") or "")
        query += "\n" + json.loads(messages[0]["content"]).get("context", "")[-600:]
    failure = result = attempt = None
    recalled = Context()
    try:
        recalled = await _cancelable(
            retrieve_context(settings, engine, task_id, user_id, project_id, "writing", query),
            engine,
            task_id,
        )
        if recalled is None:
            with Session(engine) as db, db.begin():
                task = db.get(Task, task_id)
                if task and task.status == "running":
                    task.status, task.error, task.finished_at = (
                        "failed",
                        "account_unavailable",
                        datetime.now(UTC),
                    )
            return
        with Session(engine) as db:
            check_memory_revision(db, user_id, memory_revision)
            from lisa.shared_retrieval import shared_references_current
            if not shared_references_current(db, recalled, user_id):
                raise ContextChanged
        instructions += recalled.text
        source_messages = deepcopy(messages)
        packed_stats = None
        try:
            messages, packed_stats = _pack_author_messages(settings, instructions, messages)
            trim_history(
                settings.model_copy(
                    update={"model_max_output_tokens": settings.writing_output_tokens}
                ),
                instructions,
                messages,
                0,
                schema=WritingResult,
            )
        except ModelInputTooLarge:
            instructions = snapshot["instructions"]
            recalled = Context()
            messages, packed_stats = _pack_author_messages(settings, instructions, messages)
        if packed_stats is not None:
            with Session(engine) as db, db.begin():
                current = db.get(Task, task_id)
                if current is not None and current.input_snapshot is not None:
                    snapshot = dict(current.input_snapshot)
                    snapshot.setdefault("source_messages", source_messages)
                    snapshot["messages"] = messages
                    snapshot["context_pack"] = packed_stats
                    current.input_snapshot = snapshot
        with Session(engine) as db, db.begin():
            record_references(db, task_id, recalled)
        attempt = await _cancelable(
            _write_with_character(
                settings,
                engine,
                task_id,
                user_id,
                project_id,
                instructions,
                messages,
                generate=generate,
                decide=decide,
                character_generate=character_generate,
            ),
            engine,
            task_id,
        )
        if attempt is None:
            with Session(engine) as db, db.begin():
                task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
                if task and task.status == "running":
                    task.status, task.error, task.finished_at = (
                        "failed",
                        "account_unavailable",
                        datetime.now(UTC),
                    )
            return
        result = WritingResult.model_validate(attempt.result)
        if (
            any(not p.strip() for p in result.paragraphs)
            or sum(map(len, result.paragraphs)) > snapshot["max_chars"]
        ):
            failure = "writing_scope_exceeded"
    except ContextChanged:
        failure = "context_changed"
    except CharacterUnavailable:
        failure = "character_unavailable"
    except ModelBusy:
        with Session(engine) as db, db.begin():
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status != "running":
                return
            if datetime.now(UTC) - task.created_at < timedelta(minutes=5):
                task.status = "pending"
                if not db.scalar(select(ModelCall.id).where(ModelCall.task_id == task_id)):
                    # Waiting for a model slot is not the start of autonomous writing.
                    task.started_at, task.input_snapshot = None, None
                retry = True
            else:
                task.status, task.error, task.finished_at = (
                    "failed",
                    "model_busy",
                    datetime.now(UTC),
                )
                _notice(db, task, "创作服务暂时繁忙，这次任务已停止，可在网页查看。")
                retry = False
        if retry:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3})) from None
        return
    except BudgetExceeded:
        failure = "budget_exhausted"
    except ModelCallUnknown:
        failure = "model_unknown"
    except ModelInputTooLarge:
        failure = "input_too_large"
    except ModelCallFailed:
        failure = "model_failed"
    except ValueError:
        failure = "invalid_writing_result"
    _finish_author(
        settings, engine, task_id, user_id, memory_revision, recalled, failure, result, attempt,
    )
