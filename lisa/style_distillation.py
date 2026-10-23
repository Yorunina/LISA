"""Finite corpus analysis and isolated trials; never adopt into chats, memories or works."""

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lisa.auth import DB
from lisa.model_calls import (
    BudgetExceeded,
    ModelBusy,
    ModelCallFailed,
    ModelCallUnknown,
    ModelInputTooLarge,
    generate_structured,
)
from lisa.models import Account, ModelCall, StyleCorpus, Task
from lisa.settings import Settings
from lisa.style_corpus import StyleUser, clear_corpus, require_corpus
from lisa.works import lock_account

router = APIRouter(prefix="/api/style", tags=["style distillation"])
METHOD_VERSION = "lisa-style-v1"
KINDS = {"style_distill", "style_trial"}
LIMITATIONS = (
    "模型评审不能替代用户评价，未验证多日真实交流或长篇创作效果。",
    "原始材料已清理；重新分析、核实原始证据或综合新材料需要重新上传。",
)
Short = Annotated[str, Field(min_length=1, max_length=240)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(StrictModel):
    feature: Short
    condition: Short
    source_ids: list[str] = Field(min_length=1, max_length=8)
    counterexample: str = Field(max_length=240)


class BatchAnalysis(StrictModel):
    findings: list[Finding] = Field(default_factory=list, max_length=6)
    limitations: list[Short] = Field(default_factory=list, max_length=5)


class StyleCandidate(StrictModel):
    prompt: str = Field(min_length=1, max_length=2400)
    summary: str = Field(min_length=1, max_length=600)
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    limitations: list[Short] = Field(default_factory=list, max_length=8)


class Review(StrictModel):
    observations: list[Short] = Field(default_factory=list, max_length=6)
    limitations: list[Short] = Field(default_factory=list, max_length=4)


class TrialOutput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class StartDistillation(StrictModel):
    corpus_id: UUID
    target_speakers: list[str] = Field(default_factory=list, max_length=100)
    selection: list[str] = Field(default_factory=list, max_length=10000)


class StartTrial(StrictModel):
    kind: Literal["personality", "writing"]
    prompt: str = Field(min_length=1, max_length=12000)
    input: str = Field(min_length=1, max_length=3000)


@dataclass
class AnalysisPlan:
    batches: list[list[dict]]
    development: list[dict] | None
    holdout: list[dict] | None
    coverage: dict
    limitations: list[str]
    calls: int


def _chat_probes(sources: list[dict], targets: list[str]) -> list[list[dict]]:
    """At most two turn contexts; no target answer or later target message is exposed."""
    probes, context, after_target = [], [], False
    for segment in sources:
        if segment.get("speaker") in targets:
            if context:
                probes.append(context)
                if len(probes) == 2:
                    break
                context = []
            after_target = True
        elif after_target or not probes:
            context.append({"speaker": segment.get("speaker"), "text": segment["text"]})
    return probes


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _split_segment(segment: dict, limit: int) -> list[dict]:
    """Split only oversized units, with explicit locations rather than silent truncation."""
    if _size(segment) <= limit:
        return [segment]
    text = segment["text"]
    pieces, start = [], 0
    while start < len(text):
        low, high = start + 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _size({**segment, "text": text[start:middle]}) <= limit - 100:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(text):
            # Prefer intact paragraphs/messages where the encoding budget allows it.
            boundary = text.rfind("\n", start, end)
            if boundary > start + (end - start) // 2:
                end = boundary + 1
        pieces.append(
            {
                **segment,
                "id": f"{segment['id']}@{start}:{end}",
                "parent_id": segment["id"],
                "text": text[start:end],
            }
        )
        start = end
    return pieces


def build_plan(
    payload: dict,
    kind: str,
    targets: list[str],
    selection: list[str],
    settings: Settings,
) -> AnalysisPlan:
    """Cover every selected unit; fail before any paid call if the plan cannot fit."""
    files = payload.get("files", [])
    all_segments = [segment for file in files for segment in file["segments"]]
    known = {segment["id"] for segment in all_segments}
    selected_ids = set(selection) if selection else known
    if not selected_ids or selected_ids - known:
        raise HTTPException(422, "分析范围为空或包含无法识别的片段，请重新选择。")
    speakers = {segment.get("speaker") for segment in all_segments} - {None}
    if kind == "personality" and (not targets or set(targets) - speakers):
        raise HTTPException(422, "请选择已识别的目标发言者；多文件身份需要明确勾选。")
    if kind == "personality" and not any(
        segment["id"] in selected_ids and segment.get("speaker") in targets
        for segment in all_segments
    ):
        raise HTTPException(422, "所选范围没有目标发言者的消息。")
    if settings.style_distillation_chunk_bytes > settings.style_distillation_max_input_bytes // 2:
        raise HTTPException(422, "服务端分块配置超过单次输入预算，请调整分析配置。")

    batches, groups, coverage_files, limitations = [], [], [], []
    selected_chars, selected_segments = 0, 0
    partial_context = False
    fingerprints: set[str] = set()
    duplicate_sources = False
    for file_index, file in enumerate(files):
        chosen = [s for s in file["segments"] if s["id"] in selected_ids]
        if not chosen:
            continue
        # Preserve other speakers and adjacent source order; selecting a range does not
        # filter it down to the target's standalone utterances.
        units = []
        for segment in chosen:
            source = {
                **segment,
                "file": file["name"],
                "file_index": file_index,
            }
            parts = _split_segment(source, settings.style_distillation_chunk_bytes - 256)
            partial_context |= len(parts) > 1
            units.extend(parts)
            text = segment["text"]
            if kind == "personality" and "\n" in text:
                text = text.split("\n", 1)[1]  # A changed QQ export header is not a new sample.
            normalized = _normalized(text)
            if len(normalized) >= 32:
                windows = (
                    [normalized]
                    if len(normalized) < 64
                    else [
                        normalized[start : start + 64]
                        for start in range(0, len(normalized) - 63, 32)
                    ]
                )
                hashes = {hashlib.sha256(window.encode()).hexdigest() for window in windows}
                duplicate_sources |= bool(hashes & fingerprints)
                fingerprints.update(hashes)
        file_batches, batch = [], []
        for unit in units:
            if batch and _size([*batch, unit]) > settings.style_distillation_chunk_bytes:
                file_batches.append(batch)
                batch = []
            batch.append(unit)
        if batch:
            file_batches.append(batch)
        batches.extend(file_batches)
        groups.append(file_batches)
        chars = sum(len(s["text"]) for s in chosen)
        selected_chars += chars
        selected_segments += len(chosen)
        coverage_files.append(
            {
                "name": file["name"],
                "file_index": file_index,
                "selected_segments": len(chosen),
                "analyzed_segments": len(chosen),
                "chars": chars,
                "source_ids": [s["id"] for s in chosen],
            }
        )
    development = holdout = None
    # Different whole files are the most conservative automatically available groups.
    # We do not pretend adjacent chunks of a single novel/conversation are independent.
    if len(groups) >= 3 and not duplicate_sources and len(groups[-1]) == len(groups[-2]) == 1:
        holdout, development = groups[-1][0], groups[-2][0]
        batches = batches[:-2]
        limitations.append(
            "开发与留出按完整不同文件隔离，自动检查重复片段；跨文件情节或指代依赖尚未人工核实。"
        )
        if kind == "personality" and not _chat_probes(holdout, targets):
            # There is no reply context to test in this file; do not invent one.
            batches.extend([development, holdout])
            development = holdout = None
            limitations.append("留出文件没有可用的目标接话上下文，本次未执行独立对话验证。")
    else:
        limitations.append(
            "未建立独立留出组：需至少三份可分离的非重复文件，开发与留出各须在单次预算内；"
            "同一长篇相邻章节或同一聊天的切块不冒充独立留出。"
        )
    if kind == "personality":
        limitations.append("聊天按原顺序和所选范围分批，跨批话题依赖可能影响判断。")
    if partial_context:
        limitations.append("超出单批预算的长片段已完整分段覆盖，分段处的完整语境可能不足。")
    if selected_segments < len(all_segments):
        limitations.append("仅分析用户选择的范围，未选择部分不计入风格结论。")
    total_chars = sum(len(file["text"]) for file in files)
    parsed_chars = sum(len(s["text"]) for s in all_segments)
    if total_chars > parsed_chars:
        limitations.append(
            f"有 {total_chars - parsed_chars} 字符未能归属已识别片段，未分析这些内容。"
        )
    if duplicate_sources:
        limitations.append("发现重复片段，未将重复材料分到独立验证组。")
    # Reduce observations hierarchically rather than putting an entire novel's
    # reports into one synthesis request. Count these calls before charging anything.
    count, merge_calls = len(batches), 0
    while count > 4:
        count = (count + 3) // 4
        merge_calls += count
    holdout_calls = (
        len(_chat_probes(holdout, targets)) + 1
        if holdout and kind == "personality"
        else 2
        if holdout
        else 0
    )
    calls = len(batches) + merge_calls + 1 + (1 if development else 0) + holdout_calls
    if calls > settings.style_distillation_max_calls:
        raise HTTPException(
            422,
            f"所选范围需要 {calls} 次调用，超过本任务上限 "
            f"{settings.style_distillation_max_calls} 次。请缩小选区或分批上传；未开始分析。",
        )

    def segment_count(units):
        return len({unit.get("parent_id", unit["id"]) for unit in units})

    partitions = {
        "distillation": [unit for batch in batches for unit in batch],
        "development": development or [],
        "holdout": holdout or [],
    }
    for entry in coverage_files:
        entry["role"] = next(
            role
            for role, units in partitions.items()
            if any(unit["file_index"] == entry["file_index"] for unit in units)
        )
    return AnalysisPlan(
        batches=batches,
        development=development,
        holdout=holdout,
        coverage={
            "total_segments": len(all_segments),
            "selected_segments": selected_segments,
            "analyzed_segments": selected_segments,
            "total_chars": total_chars,
            "parsed_chars": parsed_chars,
            "selected_chars": selected_chars,
            "analyzed_chars": selected_chars,
            "partitions": {
                role: {
                    "segments": segment_count(units),
                    "chars": sum(len(unit["text"]) for unit in units),
                }
                for role, units in partitions.items()
            },
            "files": coverage_files,
        },
        limitations=limitations,
        calls=calls,
    )


def _instructions(kind: str) -> str:
    name = "chat_distillation.txt" if kind == "personality" else "writing_distillation.txt"
    return (Path(__file__).parent / "prompts" / name).read_text(encoding="utf-8")


def _no_source_copy(value: BaseModel | dict, payload: dict) -> None:
    """Refuse persistence when an output contains a substantial verbatim source window."""
    data = value.model_dump() if isinstance(value, BaseModel) else value
    # Locators and filenames are metadata, not illustrative language evidence.
    clean = deepcopy(data)
    for finding in clean.get("findings", []):
        finding.pop("source_ids", None)
    rendered = _normalized(json.dumps(clean, ensure_ascii=False))
    prose = "\n".join(
        [
            str(clean.get("prompt", "")),
            str(clean.get("summary", "")),
            *[
                str(finding.get(key, ""))
                for finding in clean.get("findings", [])
                for key in ("feature", "condition", "counterexample")
            ],
            *clean.get("observations", []),
            *clean.get("limitations", []),
        ]
    )
    quoted = re.findall(r"[“「『\"]([^”」』\"\n]{3,})[”」』\"]", prose)
    windows = {rendered[index : index + 32] for index in range(max(0, len(rendered) - 31))}
    for file in payload.get("files", []):
        source = _normalized(file["text"])
        if any(source[index : index + 32] in windows for index in range(max(0, len(source) - 31))):
            raise ValueError("source_copy_detected")
        for segment in file["segments"]:
            normalized_segment = _normalized(segment["text"])
            if any(_normalized(quote) in normalized_segment for quote in quoted):
                raise ValueError("source_copy_detected")
            if segment.get("speaker") is not None:
                body = segment["text"].split("\n", 1)[-1]
                for line in body.splitlines():
                    utterance = _normalized(line)
                    if len(utterance) >= 6 and utterance in rendered:
                        raise ValueError("source_copy_detected")
            # QQ numbers/emails cannot become a persona fact, even as short substrings.
            identifiers = re.findall(r"\b\d{5,12}\b|[\w.+-]+@[\w.-]+\.\w+", segment["text"])
            if any(identifier.casefold() in rendered for identifier in identifiers):
                raise ValueError("source_identifier_detected")


def _checked_findings(value, valid_ids: set[str]) -> None:
    for finding in value.findings:
        if not set(finding.source_ids) <= valid_ids:
            raise ValueError("invalid_source_locator")


def _ensure_running(engine, task_id: UUID, user_id: UUID) -> None:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        user = db.get(Account, user_id)
        if task is None or task.status != "running" or user is None or not user.active:
            raise asyncio.CancelledError


async def _bounded_call(coroutine, engine, task_id: UUID, user_id: UUID):
    """Observe cancellation while a provider request is in flight, including late success."""
    try:
        _ensure_running(engine, task_id, user_id)
    except asyncio.CancelledError:
        coroutine.close()
        raise
    request = asyncio.create_task(coroutine)
    try:
        while not request.done():
            await asyncio.wait({request}, timeout=0.25)
            _ensure_running(engine, task_id, user_id)
        result = await request
        _ensure_running(engine, task_id, user_id)
        return result
    finally:
        if not request.done():
            request.cancel()
        # A cancellation and a provider error can race after the future has finished.
        # Consume either outcome without emitting a source-bearing task traceback.
        await asyncio.gather(request, return_exceptions=True)


async def distill(
    settings: Settings,
    engine,
    task_id: UUID,
    user_id: UUID,
    kind: str,
    payload: dict,
    targets: list[str],
    plan: AnalysisPlan,
    *,
    generate=None,
) -> dict:
    method = _instructions(kind)
    generate = generate or generate_structured

    async def call(stage, content, schema):
        value = await _bounded_call(
            generate(
                settings,
                engine,
                task_id,
                user_id,
                method + "\n本次阶段：" + stage,
                [{"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
                schema=schema,
                output_tokens=settings.style_distillation_output_tokens,
                input_byte_limit=settings.style_distillation_max_input_bytes,
            ),
            engine,
            task_id,
            user_id,
        )
        return schema.model_validate(value)

    observations, valid_ids = [], set()
    for batch in plan.batches:
        ids = {source["id"] for source in batch}
        analysis = await call(
            "分批观察，仅分析给定材料，不推断其他批次",
            {"target_speakers": targets, "source": batch},
            BatchAnalysis,
        )
        _checked_findings(analysis, ids)
        _no_source_copy(analysis, payload)
        observations.append(analysis.model_dump())
        valid_ids.update(ids)
    while len(observations) > 4:
        merged = []
        for start in range(0, len(observations), 4):
            analysis = await call(
                "跨批压缩概括，合并条件相同的规律，保留冲突和反例，不添加未见原文事实",
                {"observations": observations[start : start + 4]},
                BatchAnalysis,
            )
            _checked_findings(analysis, valid_ids)
            _no_source_copy(analysis, payload)
            merged.append(analysis.model_dump())
        observations = merged
    compact_coverage = {key: value for key, value in plan.coverage.items() if key != "files"}
    candidate = await call(
        "根据跨批概括合成精简风格，保留证据、反例和适用条件。没有看到的验证材料不得编造结论",
        {"observations": observations, "coverage": compact_coverage},
        StyleCandidate,
    )
    _checked_findings(candidate, valid_ids)
    _no_source_copy(candidate, payload)
    validation = {
        "status": "not_executed",
        "development": {"status": "not_executed"},
        "holdout": {"status": "not_executed"},
        "user_evaluation": "not_performed",
    }
    if plan.development:
        review = await call(
            "开发组审查：只评价候选适用性、反例和事实迁移，不把自评分作真人保证",
            {"candidate": candidate.prompt, "source": plan.development},
            Review,
        )
        _no_source_copy(review, payload)
        validation["development"] = {"status": "model_reviewed", **review.model_dump()}
    if plan.holdout:
        # The generator never receives any holdout reply, later turn or original prose.
        if kind == "personality":
            generated_turns = []
            probes = _chat_probes(plan.holdout, targets)
            for context in probes:
                generated = await call(
                    "冻结候选的留出接话：只提供此前他人消息及候选生成的上一轮，隐藏全部真实目标回复",
                    {
                        "candidate": candidate.prompt,
                        "context": context,
                        "previous_generated_turns": generated_turns,
                    },
                    TrialOutput,
                )
                generated_turns.append(generated.text)
        else:
            probe = {
                "instruction": "按候选笔法写一个雨后邮局失物招领的全新短场景，不续写任何来源作品。"
            }
            generated = await call(
                "冻结候选的留出生成。只给定候选和生成题，不会提供真实答案",
                {"candidate": candidate.prompt, "probe": probe},
                TrialOutput,
            )
            generated_turns = [generated.text]
        review = await call(
            "留出评审：比较候选生成的接话/笔法与留出语境，检查事实边界，不要求逐字复现",
            {"source": plan.holdout, "generated_turns": generated_turns},
            Review,
        )
        _no_source_copy(review, payload)
        validation["holdout"] = {
            "status": "generated_and_model_reviewed",
            "grouping": "whole_files_not_human_verified",
            "generated_turns": len(generated_turns),
            **review.model_dump(),
        }
        validation["status"] = "model_reviewed"
    return {
        "kind": kind,
        "prompt": candidate.prompt,
        "summary": candidate.summary,
        "report": {
            "method_version": METHOD_VERSION,
            "coverage": plan.coverage,
            "evidence": [finding.model_dump() for finding in candidate.findings],
            "limitations": [*candidate.limitations, *plan.limitations, *LIMITATIONS],
            "validation": validation,
            "source_state": "cleaned",
            "model": {"name": settings.model_name},
        },
    }


def _require_task(db: Session, user_id: UUID, task_id: UUID, *, lock=False) -> Task:
    query = select(Task).where(Task.id == task_id, Task.user_id == user_id, Task.kind.in_(KINDS))
    task = db.scalar(query.with_for_update() if lock else query)
    if task is None:
        raise HTTPException(404, "风格任务不存在。")
    return task


def task_view(db: Session, task: Task) -> dict:
    corpus_id = task.request.get("corpus_id")
    corpus = db.get(StyleCorpus, UUID(corpus_id)) if corpus_id else None
    return {
        "id": task.id,
        "kind": task.kind,
        "style_kind": task.request.get("kind"),
        "status": task.status,
        "error": task.error,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "result": task.result,
        "corpus_id": corpus_id,
        "cleanup_state": corpus.state if corpus else "not_applicable",
    }


def _limit_pending(db: Session, user_id: UUID) -> None:
    count = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.user_id == user_id, Task.kind.in_(KINDS), Task.status.in_(["pending", "running"])
        )
    )
    if count >= 2:
        raise HTTPException(409, "已有风格任务排队或执行，请等待或取消后再试。")


@router.post("/tasks", status_code=201)
def start_task(data: StartDistillation, request: Request, user: StyleUser, db: DB):
    from lisa.queue import enqueue_style

    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    _limit_pending(db, user.id)
    corpus = require_corpus(db, user.id, data.corpus_id, lock=True)
    if corpus.expires_at <= datetime.now(UTC):
        clear_corpus(db, corpus)
        db.commit()
        raise HTTPException(409, "材料已过期并清理，请重新上传。")
    if corpus.state != "ready" or not corpus.payload:
        raise HTTPException(409, "材料已使用或已清理，请重新上传。")
    plan = build_plan(
        corpus.payload,
        corpus.kind,
        data.target_speakers,
        data.selection,
        request.app.state.settings,
    )
    task = Task(
        user_id=user.id,
        kind="style_distill",
        source="web",
        status="pending",
        request={**data.model_dump(mode="json"), "kind": corpus.kind},
    )
    db.add(task)
    db.flush()
    corpus.task_id, corpus.state = task.id, "processing"
    task.queue_job_id = enqueue_style(db, task.id, user.id)
    db.commit()
    return {**task_view(db, task), "planned_calls": plan.calls, "coverage": plan.coverage}


@router.get("/tasks")
def list_tasks(user: StyleUser, db: DB):
    tasks = db.scalars(
        select(Task)
        .where(Task.user_id == user.id, Task.kind.in_(KINDS))
        .order_by(Task.created_at.desc())
        .limit(50)
    )
    return [task_view(db, task) for task in tasks]


@router.get("/tasks/{task_id}")
def get_task(task_id: UUID, user: StyleUser, db: DB):
    return task_view(db, _require_task(db, user.id, task_id))


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: UUID, user: StyleUser, db: DB):
    lock_account(db, user.id)
    task = _require_task(db, user.id, task_id, lock=True)
    if task.status in {"pending", "running"}:
        task.status, task.error, task.finished_at = "cancelled", "user_cancelled", datetime.now(UTC)
        corpus_id = task.request.get("corpus_id")
        if corpus_id:
            corpus = db.get(StyleCorpus, UUID(corpus_id))
            if corpus is not None:
                clear_corpus(db, corpus)
        if task.kind == "style_trial":
            task.request = {"kind": task.request["kind"]}
        db.commit()
    return task_view(db, task)


@router.post("/trials", status_code=201)
def start_trial(data: StartTrial, user: StyleUser, db: DB):
    from lisa.queue import enqueue_style

    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    _limit_pending(db, user.id)
    if not data.prompt.strip() or not data.input.strip():
        raise HTTPException(422, "请填写候选指引和试用内容。")
    task = Task(
        user_id=user.id,
        kind="style_trial",
        source="web",
        status="pending",
        request=data.model_dump(),
    )
    db.add(task)
    db.flush()
    task.queue_job_id = enqueue_style(db, task.id, user.id)
    db.commit()
    return task_view(db, task)


def cleanup_trials(db: Session) -> int:
    """Trials are ephemeral demonstrations; retain only task status after one day."""
    rows = db.scalars(
        select(Task)
        .where(Task.kind == "style_trial", Task.created_at < datetime.now(UTC) - timedelta(days=1))
        .with_for_update()
    )
    count = 0
    for task in rows:
        task.request = {"kind": task.request.get("kind")}
        task.result = None
        if task.status in {"pending", "running"}:
            task.status, task.error, task.finished_at = (
                "cancelled",
                "trial_expired",
                datetime.now(UTC),
            )
        count += 1
    return count


async def execute_style(settings: Settings, engine, task_id: str, *, generate=None) -> None:
    task_id = UUID(str(task_id))
    corpus_id, payload, result, failure = None, None, None, None
    with Session(engine) as db, db.begin():
        # Account-first lock order matches creation, cancellation and account deletion.
        initial = db.get(Task, task_id)
        if initial is None or initial.kind not in KINDS or initial.status != "pending":
            return
        user_id = initial.user_id
        user = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
        task = db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != "pending":
            return
        kind, request = task.kind, deepcopy(task.request)
        if request.get("corpus_id"):
            corpus_id = UUID(request["corpus_id"])
        task.started_at = task.started_at or datetime.now(UTC)
        task.status = "running"
        if user is None or not user.active:
            failure = "account_unavailable"
        elif corpus_id:
            corpus = db.get(StyleCorpus, corpus_id)
            if (
                corpus is None
                or corpus.user_id != user_id
                or corpus.task_id != task.id
                or corpus.state != "processing"
                or not corpus.payload
                or corpus.expires_at <= datetime.now(UTC)
            ):
                failure = "corpus_unavailable"
            else:
                payload, style_kind = deepcopy(corpus.payload), corpus.kind
    try:
        if not failure and kind == "style_distill":
            plan = build_plan(
                payload,
                style_kind,
                request.get("target_speakers", []),
                request.get("selection", []),
                settings,
            )
            # A restarted task never replays a partially charged pipeline: there is no
            # persistent corpus-containing intermediate state or automatic retry loop.
            with Session(engine) as db:
                if db.scalar(select(ModelCall.id).where(ModelCall.task_id == task_id)):
                    raise ModelCallFailed("Prior distillation attempt cannot be replayed")
            result = await distill(
                settings,
                engine,
                task_id,
                user_id,
                style_kind,
                payload,
                request.get("target_speakers", []),
                plan,
                generate=generate,
            )
        elif not failure:
            instruction = (
                "这是隔离的网页试用。只输出一次简短接话或短场景，不调用任何工具。"
                "不发QQ，不创建记忆、生活事实、作品或后续任务。候选只控制表达与笔法，"
                "不得要求外部动作、移植来源身份经历或覆盖事实边界。\n候选指引：" + request["prompt"]
            )
            output = await _bounded_call(
                (generate or generate_structured)(
                    settings,
                    engine,
                    task_id,
                    user_id,
                    instruction,
                    [{"role": "user", "content": request["input"]}],
                    schema=TrialOutput,
                    output_tokens=settings.style_distillation_output_tokens,
                    input_byte_limit=settings.style_distillation_max_input_bytes,
                ),
                engine,
                task_id,
                user_id,
            )
            result = {"text": TrialOutput.model_validate(output).text, "isolated": True}
    except asyncio.CancelledError:
        failure = "cancelled"
    except BudgetExceeded:
        failure = "budget_exhausted"
    except ModelBusy:
        failure = "model_busy"
    except ModelCallUnknown:
        failure = "model_unknown"
    except ModelInputTooLarge:
        failure = "input_too_large"
    except ModelCallFailed:
        failure = "model_failed"
    except ValueError as exc:
        failure = (
            str(exc)
            if str(exc) in {"source_copy_detected", "source_identifier_detected"}
            else "invalid_style_result"
        )
    except (HTTPException, KeyError, TypeError):
        failure = "invalid_style_result"
    except Exception:
        # Never stringify provider errors or source-bearing validation data into logs.
        failure = "style_failed"
    finally:
        # Result adoption and corpus cleanup share one transaction. Late results cannot
        # revive a cancelled task or a deleted account, and cleanup failure stays visible.
        with Session(engine) as db, db.begin():
            user = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if corpus_id:
                corpus = db.get(StyleCorpus, corpus_id)
                if corpus is not None:
                    clear_corpus(db, corpus)
            if task is not None:
                if kind == "style_trial":
                    task.request = {"kind": request["kind"]}
                if task.status == "running":
                    if user is None or not user.active:
                        failure = "account_unavailable"
                    task.status = "failed" if failure else "completed"
                    task.error, task.finished_at = failure, datetime.now(UTC)
                    if not failure:
                        task.result = result
        payload = None
