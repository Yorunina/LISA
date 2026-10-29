"""Deployment-allowlisted, body-free operations views over existing business facts."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import (
    Date,
    Float,
    Integer,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from lisa.auth import DB, current_user
from lisa.models import (
    Account,
    ActivityPlan,
    Chapter,
    Character,
    IndexState,
    Memory,
    ModelCall,
    OutboundMessage,
    Project,
    RagEvaluationRun,
    RetrievalObservation,
    RuntimeHeartbeat,
    SharedEntry,
    Task,
)
from lisa.retrieval import _blocked_diary, embedding_space

router = APIRouter(prefix="/api/operations", tags=["operations"])
PAGE_SIZE = 25
KNOWN_COSTS = ("provider", "estimate", "conservative")


def allowed(user, settings):
    names = {x.strip().lower() for x in settings.operations_maintainers.split(",") if x.strip()}
    return user.username.lower() in names or str(user.id).lower() in names


def maintainer(request: Request, user=Depends(current_user)):
    if not allowed(user, request.app.state.settings):
        raise HTTPException(403, "此运行页面仅对部署指定的维护者开放。")
    return user


@router.get("/access")
def access(request: Request, user=Depends(current_user)):
    return {"allowed": allowed(user, request.app.state.settings)}


@router.get("/napcat/open", response_class=RedirectResponse)
def open_napcat(request: Request, user: Annotated[Account, Depends(maintainer)]):
    from lisa.napcat_status import management_url

    return RedirectResponse(
        management_url(request.app.state.settings), status_code=303,
        headers={
            "Cache-Control": "no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, noarchive",
        },
    )


def period(settings, kind, start=None, end=None, *, now=None):
    now = now or datetime.now(UTC)
    zone = ZoneInfo(settings.operations_timezone)
    today = now.astimezone(zone).date()
    if kind == "custom":
        if start is None or end is None or start > end:
            raise HTTPException(422, "请提供有效的开始日和结束日。")
        if (end - start).days > 92:
            raise HTTPException(422, "一次最多查询93个自然日。")
    else:
        start = today - timedelta(days={"today": 0, "7d": 6, "30d": 29}[kind])
        end = today
    return (
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def _count(condition):
    return func.count().filter(condition)


def _number(value):
    return float(value) if value is not None else None


def _latency(db, source, value):
    row = db.execute(
        select(
            func.percentile_cont(0.5).within_group(value),
            func.percentile_cont(0.95).within_group(value),
            func.count(value),
        ).select_from(source)
    ).one()
    return dict(median_ms=_number(row[0]), p95_ms=_number(row[1]), samples=row[2])


def _states(db, source, column):
    return [
        dict(name=name or "unknown", count=count)
        for name, count in db.execute(
            select(column, func.count()).select_from(source).group_by(column).order_by(column)
        )
    ]


def _choices(db, expression):
    return list(
        db.scalars(
            select(expression)
            .where(expression.is_not(None))
            .distinct()
            .order_by(expression)
            .limit(200)
        )
    )


def _call_source(start, end, purpose, model, task_kind, state):
    config = ModelCall.configuration
    # Historical rows lack an authoritative sent marker and stay unknown.
    dispatch = func.coalesce(config["dispatch_state"].astext, "unknown")
    usage_known = and_(ModelCall.input_tokens.is_not(None), ModelCall.output_tokens.is_not(None))
    cost_source = case(
        (ModelCall.state == "reserved", "inflight"),
        (config["cost_source"].astext.in_((*KNOWN_COSTS, "unknown")), config["cost_source"].astext),
        (
            or_(config["price_known"].astext == "false", ModelCall.error == "price_unavailable"),
            "unknown",
        ),
        (and_(ModelCall.charged_usd == 0, config["price_known"].astext.is_(None)), "unknown"),
        (
            and_(
                ModelCall.charged_usd.is_not(None), or_(ModelCall.state == "unknown", ~usage_known)
            ),
            "conservative",
        ),
        (and_(usage_known, ModelCall.charged_usd.is_not(None)), "estimate"),
        else_="unknown",
    )
    query = (
        select(
            ModelCall.id,
            ModelCall.task_id,
            Task.kind.label("task_kind"),
            ModelCall.state,
            func.coalesce(config["purpose"].astext, "unknown").label("purpose"),
            func.coalesce(config["model"].astext, "unknown").label("model"),
            dispatch.label("dispatch_state"),
            cost_source.label("cost_source"),
            ModelCall.charged_usd.label("amount"),
            ModelCall.reserved_usd,
            ModelCall.input_tokens,
            ModelCall.output_tokens,
            ModelCall.started_at,
            ModelCall.finished_at,
            ModelCall.error,
            case(
                (
                    ModelCall.finished_at.is_not(None),
                    func.greatest(
                        0, func.extract("epoch", ModelCall.finished_at - ModelCall.started_at)
                    )
                    * 1000,
                ),
                else_=None,
            ).label("duration_ms"),
        )
        .join(Task, Task.id == ModelCall.task_id)
        .where(
            ModelCall.started_at >= start,
            ModelCall.started_at < end,
        )
    )
    for condition, value in (
        (config["purpose"].astext == purpose, purpose),
        (config["model"].astext == model, model),
        (Task.kind == task_kind, task_kind),
        (ModelCall.state == state, state),
    ):
        if value:
            query = query.where(condition)
    return query.subquery()


def _calls(db, source, zone, page):
    c = source.c
    closed = c.finished_at.is_not(None)
    is_known = c.cost_source.in_(KNOWN_COSTS)
    amount = case((is_known, func.coalesce(c.amount, 0)), else_=0)
    total = db.execute(
        select(
            func.count(),
            _count(c.dispatch_state == "sent"),
            _count(c.dispatch_state == "not_sent"),
            _count(c.dispatch_state.in_(["unknown", "dispatching"])),
            _count(c.state == "reserved"),
            _count(and_(closed, c.dispatch_state == "sent", c.state == "succeeded")),
            _count(and_(closed, c.dispatch_state == "sent")),
            func.sum(c.input_tokens),
            func.sum(c.output_tokens),
            _count(or_(c.input_tokens.is_(None), c.output_tokens.is_(None))),
            func.coalesce(func.sum(amount), 0),
            *[
                func.coalesce(func.sum(case((c.cost_source == kind, c.amount), else_=0)), 0)
                for kind in KNOWN_COSTS
            ],
            _count(c.cost_source == "unknown"),
            func.coalesce(func.sum(case((c.state == "reserved", c.reserved_usd), else_=0)), 0),
            func.count(func.distinct(c.task_id)),
        ).select_from(source)
    ).one()
    purpose_rows = []
    for row in db.execute(
        select(
            c.purpose,
            func.count(),
            _count(c.state == "succeeded"),
            _count(c.state == "failed"),
            _count(c.state == "unknown"),
            func.sum(c.input_tokens),
            func.sum(c.output_tokens),
            _count(or_(c.input_tokens.is_(None), c.output_tokens.is_(None))),
            func.sum(amount),
            _count(c.cost_source == "unknown"),
            func.percentile_cont(0.5).within_group(c.duration_ms),
            func.percentile_cont(0.95).within_group(c.duration_ms),
            func.count(c.duration_ms),
        )
        .group_by(c.purpose)
        .order_by(c.purpose)
    ):
        purpose_rows.append(
            dict(
                zip(
                    [
                        "purpose",
                        "count",
                        "succeeded",
                        "failed",
                        "unknown",
                        "input_tokens",
                        "output_tokens",
                        "unknown_tokens",
                        "known_amount",
                        "unknown_costs",
                        "median_ms",
                        "p95_ms",
                        "latency_samples",
                    ],
                    row,
                    strict=True,
                )
            )
        )
    local_day = cast(func.timezone(zone, c.started_at), Date)
    daily = [
        dict(day=row[0], count=row[1], known_amount=row[2], unknown_count=row[3])
        for row in db.execute(
            select(local_day, func.count(), func.sum(amount), _count(c.cost_source == "unknown"))
            .group_by(local_day)
            .order_by(local_day)
        )
    ]
    recent = [
        dict(row)
        for row in db.execute(
            select(source)
            .order_by(c.started_at.desc(), c.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        ).mappings()
    ]
    has_more = len(recent) > PAGE_SIZE
    for row in recent:
        if row["cost_source"] not in KNOWN_COSTS:
            row["amount"] = None
        row.pop("duration_ms", None)
        row.pop("reserved_usd", None)
    return (
        dict(
            total=total[0],
            confirmed_sent=total[1],
            not_sent=total[2],
            unknown_dispatch=total[3],
            inflight=total[4],
            success_rate=total[5] / total[6] if total[6] else None,
            latency=_latency(db, source, c.duration_ms),
            tokens=dict(input=total[7], output=total[8], unknown=total[9]),
            by_purpose=purpose_rows,
            daily=daily,
            recent=recent[:PAGE_SIZE],
        ),
        dict(
            currency="CNY",
            known_total=total[10],
            provider=total[11],
            estimate=total[12],
            conservative=total[13],
            unknown_count=total[14],
            inflight_reserved=total[15],
            incomplete=bool(total[14] or total[13] or total[4]),
            per_task_average=total[10] / total[16] if total[16] else None,
            ledger_note="可计价合计按调用发起日统计；含估算/待确认金额，不是完整供应商账单。"
            "账号删除后的无身份旧日账不用于恢复个人明细。",
        ),
        has_more,
    )


def _tasks(db, start, end, task_kind, state, model, purpose, now, page):
    query = select(Task)
    if task_kind:
        query = query.where(Task.kind == task_kind)
    if model or purpose or state:
        sub = select(ModelCall.id).where(ModelCall.task_id == Task.id)
        if model:
            sub = sub.where(ModelCall.configuration["model"].astext == model)
        if purpose:
            sub = sub.where(ModelCall.configuration["purpose"].astext == purpose)
        if state:
            sub = sub.where(ModelCall.state == state)
        query = query.where(sub.exists())
    created = query.where(Task.created_at >= start, Task.created_at < end).subquery()
    finished = query.where(Task.finished_at >= start, Task.finished_at < end).subquery()
    closed_count = db.scalar(select(func.count()).select_from(finished))
    states = _states(db, finished, finished.c.status)
    succeeded = sum(x["count"] for x in states if x["name"] == "completed")
    queue_rows = db.execute(
        select(Task.kind, Task.status, Task.created_at, Task.request).where(
            Task.status.in_(["pending", "running", "cancel_requested"])
        )
    )
    queue = dict(pending=0, running=0, debouncing=0, scheduled=0, oldest_wait_seconds=None)
    waits = []
    for kind, status, created_at, request in queue_rows:
        if status != "pending":
            queue["running"] += 1
            continue
        deadline = request.get("chat_silence_until") or request.get("not_before")
        try:
            deadline = datetime.fromisoformat(deadline) if isinstance(deadline, str) else None
        except ValueError:
            deadline = None
        if deadline and deadline.tzinfo and deadline > now:
            queue["debouncing" if kind == "chat" else "scheduled"] += 1
        else:
            queue["pending"] += 1
            eligible_at = max(created_at, deadline) if deadline and deadline.tzinfo else created_at
            waits.append(max(0, (now - eligible_at).total_seconds()))
    queue["oldest_wait_seconds"] = max(waits, default=None)
    for due_at in db.scalars(
        select(ActivityPlan.due_at).where(
            ActivityPlan.status == "pending",
            ActivityPlan.task_id.is_(None),
        )
    ):
        if due_at > now:
            queue["scheduled"] += 1
        else:
            queue["pending"] += 1
            waits.append(max(0, (now - due_at).total_seconds()))
    queue["oldest_wait_seconds"] = max(waits, default=None)
    rows = list(
        db.scalars(
            query.where(Task.created_at >= start, Task.created_at < end)
            .order_by(Task.created_at.desc(), Task.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        )
    )
    recent = [
        dict(
            id=t.id,
            kind=t.kind,
            status=t.status,
            created_at=t.created_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
            error=t.error,
            adopted=t.adopted_at is not None,
        )
        for t in rows[:PAGE_SIZE]
    ]
    wait_ms = case(
        (
            created.c.started_at.is_not(None),
            func.greatest(0, func.extract("epoch", created.c.started_at - created.c.created_at))
            * 1000,
        )
    )
    execution_ms = case(
        (
            finished.c.started_at.is_not(None),
            func.greatest(0, func.extract("epoch", finished.c.finished_at - finished.c.started_at))
            * 1000,
        )
    )
    delivery_counts = dict(
        db.execute(
            select(OutboundMessage.status, func.count())
            .where(OutboundMessage.created_at >= start, OutboundMessage.created_at < end)
            .group_by(OutboundMessage.status)
        ).all()
    )
    delivery = dict(
        pending=delivery_counts.get("pending", 0) + delivery_counts.get("sending", 0),
        succeeded=delivery_counts.get("sent", 0),
        failed=delivery_counts.get("failed", 0),
        unknown=delivery_counts.get("unknown", 0),
        partial_tasks=0,
    )
    partial = (
        select(OutboundMessage.task_id)
        .where(
            OutboundMessage.created_at >= start,
            OutboundMessage.created_at < end,
        )
        .group_by(OutboundMessage.task_id)
        .having(
            and_(
                func.count().filter(OutboundMessage.status == "sent") > 0,
                func.count().filter(OutboundMessage.status != "sent") > 0,
            )
        )
        .subquery()
    )
    delivery["partial_tasks"] = db.scalar(select(func.count()).select_from(partial))
    delivered = (
        select(
            OutboundMessage.task_id.label("task_id"),
            func.max(OutboundMessage.finished_at).label("delivered_at"),
        )
        .group_by(OutboundMessage.task_id)
        .having(func.bool_and(OutboundMessage.status == "sent"))
        .subquery()
    )
    end_to_end = (
        select(
            (
                func.greatest(0, func.extract("epoch", delivered.c.delivered_at - Task.created_at))
                * 1000
            ).label("duration_ms")
        )
        .join(delivered, delivered.c.task_id == Task.id)
        .where(Task.kind == "chat", Task.created_at >= start, Task.created_at < end)
        .subquery()
    )
    return (
        dict(
            created=db.scalar(select(func.count()).select_from(created)),
            finished=closed_count,
            states=states,
            success_rate=succeeded / closed_count if closed_count else None,
            wait=_latency(db, created, wait_ms),
            execution=_latency(db, finished, execution_ms),
            end_to_end=_latency(db, end_to_end, end_to_end.c.duration_ms),
            delivery=delivery,
            recent=recent,
        ),
        queue,
        len(rows) > PAGE_SIZE,
    )


def _coverage(db, settings):
    """Current source versions, including never-indexed sources, not just index rows."""
    space = embedding_space(settings)
    project_enabled = func.coalesce(
        Project.retrieval_config["sources"], cast(["memory", "project", "chapter"], JSONB)
    )
    writing_scope = (
        select(Project.id)
        .where(
            Project.user_id == Memory.user_id,
            or_(Memory.project_id.is_(None), Project.id == Memory.project_id),
            project_enabled.contains(["memory"]),
        )
        .exists()
    )
    chat_scope = or_(
        Memory.project_id.is_(None),
        select(Project.id)
        .where(
            Project.user_id == Memory.user_id,
            Project.id == Memory.project_id,
            Project.is_current,
        )
        .exists(),
    )
    memory = select(
        literal("memory").label("kind"),
        Memory.id.label("source_id"),
        Memory.revision,
        (Memory.allow_chat & chat_scope).label("allow_chat"),
        (Memory.allow_writing & writing_scope).label("allow_writing"),
        literal(False).label("role"),
    )
    memory = memory.join(Account, Account.id == Memory.user_id).where(
        Account.active,
        Memory.active,
        or_(Memory.allow_chat & chat_scope, Memory.allow_writing & writing_scope),
    )
    projects = select(
        literal("project"),
        Project.id,
        Project.index_revision,
        literal(False),
        literal(True),
        literal(False),
    )
    projects = projects.join(Account, Account.id == Project.user_id).where(
        Account.active,
        project_enabled.contains(["project"]),
        or_(
            Project.worldbook != "",
            Project.characters != "",
            Project.preferences != "",
            Project.tentative != "",
            func.jsonb_array_length(Project.worldbook_entries) > 0,
        ),
    )
    chapters = select(
        literal("chapter"),
        Chapter.id,
        Chapter.version,
        literal(False),
        literal(True),
        literal(False),
    )
    chapters = (
        chapters.join(Project)
        .join(Account, Account.id == Project.user_id)
        .where(
            Account.active,
            project_enabled.contains(["chapter"]),
            ~_blocked_diary(Chapter.id, Chapter.version, Project.user_id),
            Chapter.content != {"type": "doc", "content": []},
        )
    )
    roles = select(
        literal("character"),
        Character.id,
        Character.revision,
        literal(False),
        literal(False),
        literal(True),
    )
    roles = (
        roles.join(Project)
        .join(Account, Account.id == Project.user_id)
        .where(
            Account.active,
            Character.enabled,
            Character.known_information != "",
            func.coalesce(Character.retrieval_config["enabled"].astext, "true") == "true",
        )
    )
    sources = union_all(memory, projects, chapters, roles).subquery()
    state = IndexState
    index_task = aliased(Task)
    failed_index = index_task.status.in_(["failed", "outcome_unknown"])
    joined = sources.outerjoin(
        state, and_(state.source_kind == sources.c.kind, state.source_id == sources.c.source_id)
    )
    result = []
    joined = joined.outerjoin(index_task, state.task_id == index_task.id)
    for purpose, flag in (
        ("chat", sources.c.allow_chat),
        ("writing", sources.c.allow_writing),
        ("role", sources.c.role),
    ):
        row = db.execute(
            select(
                func.count(),
                _count(
                    and_(state.revision == sources.c.revision, state.space == space, state.complete)
                ),
                _count(
                    and_(
                        state.id.is_not(None),
                        or_(state.revision != sources.c.revision, state.space != space),
                    )
                ),
                _count(
                    and_(
                        state.revision == sources.c.revision,
                        state.space == space,
                        ~state.complete,
                        or_(index_task.status.is_(None), ~failed_index),
                    )
                ),
                _count(state.id.is_(None)),
                _count(
                    and_(
                        state.revision == sources.c.revision,
                        state.space == space,
                        ~state.complete,
                        failed_index,
                    )
                ),
            )
            .select_from(joined)
            .where(flag)
        ).one()
        result.append(
            dict(
                zip(
                    ["purpose", "total", "complete", "stale", "partial", "missing", "failed"],
                    [purpose, *row],
                    strict=True,
                )
            )
        )
    public = db.execute(
        select(
            func.count(),
            _count(
                and_(state.revision == SharedEntry.revision, state.space == space, state.complete)
            ),
            _count(
                and_(
                    state.id.is_not(None),
                    or_(state.revision != SharedEntry.revision, state.space != space),
                )
            ),
            _count(
                and_(
                    state.revision == SharedEntry.revision,
                    state.space == space,
                    ~state.complete,
                    or_(index_task.status.is_(None), ~failed_index),
                )
            ),
            _count(state.id.is_(None)),
            _count(
                and_(
                    state.revision == SharedEntry.revision,
                    state.space == space,
                    ~state.complete,
                    failed_index,
                )
            ),
        )
        .select_from(SharedEntry)
        .outerjoin(state, and_(state.source_kind == "shared", state.source_id == SharedEntry.id))
        .outerjoin(index_task, state.task_id == index_task.id)
        .where(SharedEntry.status == "active", SharedEntry.allow_chat)
    ).one()
    result.append(
        dict(
            zip(
                ["purpose", "total", "complete", "stale", "partial", "missing", "failed"],
                ["shared", *public],
                strict=True,
            )
        )
    )
    return result


def _rag(db, start, end, settings, page, call_source, filtered, task_kind):
    query = select(RetrievalObservation).where(
        RetrievalObservation.created_at >= start,
        RetrievalObservation.created_at < end,
    )
    if filtered:
        query = query.where(RetrievalObservation.task_id.in_(select(call_source.c.task_id)))
    elif task_kind:
        query = query.where(
            RetrievalObservation.task_id.in_(select(Task.id).where(Task.kind == task_kind))
        )
    source = query.subquery()
    c = source.c
    total = db.scalar(select(func.count()).select_from(source))
    states = _states(db, source, c.metrics["state"].astext)
    routes = _states(db, source, c.metrics["route"].astext)
    executed_count = sum(
        x["count"]
        for x in states
        if x["name"]
        in {
            "executed",
            "empty",
            "fallback",
        }
    )
    empty_count = sum(x["count"] for x in states if x["name"] == "empty")
    public_counts = db.execute(
        select(
            *[
                func.coalesce(func.sum(cast(c.metrics["public"][key].astext, Integer)), 0)
                for key in ("candidates", "assembled", "final_count", "final_chars")
            ]
        ).select_from(source)
    ).one()
    counts = db.execute(
        select(
            *[
                func.coalesce(func.sum(cast(c.metrics[key].astext, Integer)), 0)
                for key in ("candidates", "assembled", "final_count", "final_chars")
            ]
        ).select_from(source)
    ).one()
    # Only known counter fields are read, never arbitrary JSON/query text.
    reasons = func.jsonb_each_text(c.metrics["omitted"]).table_valued("key", "value").lateral()
    reason_counts = db.execute(
        select(reasons.c.key, func.sum(cast(reasons.c.value, Integer)))
        .select_from(source)
        .join(reasons, true())
        .group_by(reasons.c.key)
        .limit(50)
    ).all()
    recent = list(
        db.execute(
            select(source)
            .order_by(c.created_at.desc(), c.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        ).mappings()
    )
    safe_recent = []
    for row in recent[:PAGE_SIZE]:
        metrics = row["metrics"]
        safe_recent.append(
            dict(
                id=row["id"],
                purpose=row["purpose"],
                created_at=row["created_at"],
                metrics={
                    k: metrics[k]
                    for k in (
                        "state",
                        "route",
                        "durations_ms",
                        "candidates",
                        "deduped",
                        "assembled",
                        "assembled_chars",
                        "final_count",
                        "final_chars",
                        "omitted",
                    )
                    if k in metrics
                },
            )
        )
    evaluation = db.scalar(
        select(RagEvaluationRun).order_by(RagEvaluationRun.created_at.desc()).limit(1)
    )
    return dict(
        observations=total,
        executed_count=executed_count,
        empty_count=empty_count,
        empty_result_rate=empty_count / executed_count if executed_count else None,
        states=states,
        routes=routes,
        counts=dict(
            zip(["candidates", "assembled", "final_count", "final_chars"], counts, strict=True)
        ),
        omitted=[dict(name=k, count=v) for k, v in sorted(reason_counts)],
        latency=_latency(db, source, cast(c.metrics["durations_ms"]["total"].astext, Float)),
        latencies={
            key: _latency(db, source, cast(c.metrics["durations_ms"][key].astext, Float))
            for key in ("embedding", "retrieval", "assembly")
        },
        public_counts=dict(
            zip(
                ["candidates", "assembled", "final_count", "final_chars"],
                public_counts,
                strict=True,
            )
        ),
        coverage=_coverage(db, settings),
        recent=safe_recent,
        evaluation=dict(
            id=evaluation.id,
            created_at=evaluation.created_at,
            configuration=evaluation.configuration,
            results=evaluation.results,
        )
        if evaluation
        else None,
    ), len(recent) > PAGE_SIZE


def _components(db, settings, now):
    rows = {r.component: r for r in db.scalars(select(RuntimeHeartbeat))}
    result = []
    for key, label in (
        ("api", "网页 API"),
        ("database", "数据库"),
        ("worker", "任务 worker"),
        ("image_worker", "图片 worker"),
        ("napcat", "NapCat"),
        ("gateway", "QQ 网关"),
        ("qq", "QQ 会话"),
    ):
        row = rows.get(key)
        age = max(0, (now - row.observed_at).total_seconds()) if row else None
        status = "ok" if key in {"api", "database"} else "unknown"
        if row and key not in {"api", "database"}:
            status = "stale" if age > settings.operations_stale_seconds else row.status
            if key == "qq" and status == "ok":
                login = row.details.get("logged_in")
                status = "ok" if login is True else "offline" if login is False else "unknown"
        details = dict(row.details) if row else {}
        if key == "napcat":
            details["webui_url"] = settings.napcat_webui_url
            details["open_url"] = "/api/operations/napcat/open"
        result.append(
            dict(
                component=key,
                label=label,
                status=status,
                observed_at=row.observed_at if row else now if key in {"api", "database"} else None,
                age_seconds=age,
                details=details,
            )
        )
    return result


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: DB,
    user: Annotated[Account, Depends(maintainer)],
    range: Literal["today", "7d", "30d", "custom"] = "today",
    start: date | None = None,
    end: date | None = None,
    purpose: str | None = Query(default=None, max_length=64),
    model: str | None = Query(default=None, max_length=160),
    task_kind: str | None = Query(default=None, max_length=24),
    state: str | None = Query(default=None, max_length=24),
    page: int = Query(default=1, ge=1, le=4000),
):
    settings = request.app.state.settings
    now = datetime.now(UTC)
    lower, upper = period(settings, range, start, end, now=now)
    source = _call_source(lower, upper, purpose, model, task_kind, state)
    calls, costs, more_calls = _calls(db, source, settings.operations_timezone, page)
    tasks, queue, more_tasks = _tasks(db, lower, upper, task_kind, state, model, purpose, now, page)
    rag, more_rag = _rag(
        db, lower, upper, settings, page, source, bool(model or purpose or state), task_kind
    )
    components = _components(db, settings, now)
    alerts = []
    for component in components:
        if component["status"] in {"stale", "offline"}:
            alerts.append(
                dict(
                    code=component["component"],
                    severity="error",
                    message=component["label"] + "已离线或心跳过期。",
                    observed_at=now,
                )
            )
    if queue["oldest_wait_seconds"] and (
        queue["oldest_wait_seconds"] > settings.operations_task_warning_seconds
    ):
        alerts.append(
            dict(
                code="queue_wait",
                severity="warning",
                message="可执行任务持续积压。",
                observed_at=now,
            )
        )
    if costs["incomplete"]:
        alerts.append(
            dict(
                code="cost_incomplete",
                severity="warning",
                message="费用包含待确认或无法计价调用，总体消费尚不完整。",
                observed_at=now,
            )
        )
    if calls["unknown_dispatch"]:
        alerts.append(
            dict(
                code="dispatch_unknown",
                severity="warning",
                message="存在发送结果待确认或缺少历史发送记录的调用。",
                observed_at=now,
            )
        )
    unknown_outcomes = db.scalar(
        select(func.count()).select_from(source).where(source.c.state == "unknown")
    )
    long_tasks = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.status.in_(["running", "cancel_requested"]),
            Task.started_at < now - timedelta(seconds=settings.operations_task_warning_seconds),
        )
    )
    recent_calls = (
        select(ModelCall.state)
        .where(
            ModelCall.started_at >= now - timedelta(minutes=15),
            ModelCall.finished_at.is_not(None),
        )
        .subquery()
    )
    failed_count, recent_count = db.execute(
        select(
            _count(recent_calls.c.state.in_(["failed", "unknown"])),
            func.count(),
        ).select_from(recent_calls)
    ).one()
    for code, active, message in (
        ("unknown_outcome", unknown_outcomes, "存在外部模型结果未知的调用，请核对而非盲目重发。"),
        ("long_task", long_tasks, "存在执行时间较长的任务。"),
        (
            "model_errors",
            recent_count >= 5 and failed_count / recent_count >= 0.5,
            "最近15分钟模型异常占比偏高（至少5项，失败/未知不少于一半）。",
        ),
        (
            "index_backlog",
            any(r["stale"] or r["partial"] or r["missing"] for r in rag["coverage"]),
            "部分有效资料尚未完成当前版本索引。",
        ),
    ):
        if active:
            alerts.append(dict(code=code, severity="warning", message=message, observed_at=now))
    return dict(
        generated_at=now,
        timezone=settings.operations_timezone,
        refresh_seconds=settings.operations_refresh_seconds,
        range=dict(start=lower, end=upper, label=range),
        components=components,
        queue=queue,
        coverage=dict(
            started_at=db.scalar(select(func.min(ModelCall.started_at))),
            total_calls=calls["total"],
            classified_calls=sum(
                r["count"] for r in calls["by_purpose"] if r["purpose"] != "unknown"
            ),
            unknown_dispatch=calls["unknown_dispatch"],
            history_note="历史用途/发送阶段缺失单列未知；旧账单不等于供应商实际金额。",
        ),
        tasks=tasks,
        calls=calls,
        costs=costs,
        rag=rag,
        alerts=alerts,
        filters=dict(
            purposes=_choices(db, ModelCall.configuration["purpose"].astext),
            models=_choices(db, ModelCall.configuration["model"].astext),
            task_kinds=_choices(db, Task.kind),
            states=_choices(db, ModelCall.state),
        ),
        pagination=dict(
            page=page,
            page_size=PAGE_SIZE,
            has_more_calls=more_calls,
            has_more_tasks=more_tasks,
            has_more_retrieval=more_rag,
        ),
    )
