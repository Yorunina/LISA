"""Passive, authorized group archives. Nothing in this module sends QQ messages."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.models import Account, CollectedGroup, GroupGrant, GroupMessage, StyleCorpus, Task
from lisa.style_corpus import clear_corpus, corpus_view
from lisa.works import lock_account

router = APIRouter(prefix="/api/groups", tags=["group records"])
GroupUser = Annotated[Account, Depends(current_user)]
RETENTION = timedelta(days=30)
WINDOW_GAP = timedelta(minutes=30)
MAX_MESSAGE_BYTES = 262_144


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupInput(StrictModel):
    bot_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    qq_group_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip():
            raise ValueError("群名称不能为空。")
        return value.strip()

    @field_validator("bot_id", "qq_group_id")
    @classmethod
    def positive_id(cls, value):
        if int(value) <= 0:
            raise ValueError("QQ 号码须为正数。")
        return str(int(value))


class GroupPatch(StrictModel):
    enabled: bool
    name: str | None = Field(default=None, min_length=1, max_length=120)


class GrantInput(StrictModel):
    username: str = Field(min_length=3, max_length=32)


class CorpusQuery(StrictModel):
    qq_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    keyword: str | None = Field(default=None, max_length=200)
    start: datetime | None = None
    end: datetime | None = None


def _group_lock(db: Session, group_id: UUID) -> None:
    # All group-source mutation paths take this before accounts. Worker guards are
    # read-only and never acquire it while holding an account/task lock.
    key = int.from_bytes(group_id.bytes[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _locked_user(db: Session, user_id: UUID) -> Account:
    user = lock_account(db, user_id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    return user


def _is_admin(db: Session, user_id: UUID) -> bool:
    return bool(
        db.scalar(select(Account.is_admin).where(Account.id == user_id, Account.active.is_(True)))
    )


def _require_admin(db: Session, user_id: UUID) -> None:
    if not _is_admin(db, user_id):
        raise HTTPException(403, "只有管理员可以管理群采集与授权。")


def require_group(db: Session, user_id: UUID, group_id: UUID) -> CollectedGroup:
    group = db.scalar(
        select(CollectedGroup)
        .where(CollectedGroup.id == group_id)
        .execution_options(populate_existing=True)
    )
    if group is None or (
        not _is_admin(db, user_id) and db.get(GroupGrant, (group_id, user_id)) is None
    ):
        raise HTTPException(404, "群记录不存在或未获访问授权。")
    return group


def _bounds(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if any(value is not None and value.tzinfo is None for value in (start, end)):
        raise HTTPException(422, "时间范围须包含时区。")
    if start and end and start > end:
        raise HTTPException(422, "开始时间不能晚于结束时间。")
    return max(start or now - RETENTION, now - RETENTION), min(end or now, now)


def _filters(group_id: UUID, start: datetime, end: datetime, qq_id=None, keyword=None):
    clauses = [
        GroupMessage.group_id == group_id,
        GroupMessage.sent_at >= start,
        GroupMessage.sent_at <= end,
    ]
    if qq_id:
        clauses.append(GroupMessage.qq_id == qq_id)
    if keyword:
        # Literal substring search; percent/underscore are not wildcard privileges.
        clauses.append(GroupMessage.content.contains(keyword, autoescape=True))
    return clauses


def _group_view(db: Session, group: CollectedGroup) -> dict:
    cutoff = datetime.now(UTC) - RETENTION
    count, first, last = db.execute(
        select(func.count(), func.min(GroupMessage.sent_at), func.max(GroupMessage.sent_at)).where(
            GroupMessage.group_id == group.id, GroupMessage.sent_at >= cutoff
        )
    ).one()
    return {
        "id": group.id,
        "bot_id": group.bot_id,
        "qq_group_id": group.qq_group_id,
        "name": group.name,
        "enabled": group.enabled,
        "message_count": count,
        "first_at": first,
        "last_at": last,
    }


def _invalidate_corpora(
    db: Session, *, group_id: UUID | None = None, user_id: UUID | None = None, expired_only=False
) -> int:
    query = select(StyleCorpus.id, StyleCorpus.user_id).where(
        StyleCorpus.source_group_id.is_not(None), StyleCorpus.cleaned_at.is_(None)
    )
    if group_id:
        query = query.where(StyleCorpus.source_group_id == group_id)
    if user_id:
        query = query.where(StyleCorpus.user_id == user_id)
    if expired_only:
        query = query.where(StyleCorpus.expires_at <= datetime.now(UTC))
    candidates = list(db.execute(query.order_by(StyleCorpus.user_id, StyleCorpus.id)))
    count = 0
    for corpus_id, owner_id in candidates:
        # Advisory group mutation lock (if applicable) precedes this common order.
        owner = db.scalar(select(Account).where(Account.id == owner_id).with_for_update())
        if owner is None:
            continue
        # A task may have been attached while this invalidation waited for its
        # account lock. Read the current reference only after owning that lock.
        task_id = db.scalar(select(StyleCorpus.task_id).where(StyleCorpus.id == corpus_id))
        task = (
            db.scalar(select(Task).where(Task.id == task_id).with_for_update()) if task_id else None
        )
        corpus = db.scalar(
            select(StyleCorpus)
            .where(StyleCorpus.id == corpus_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if corpus is None or corpus.cleaned_at:
            continue
        if task and task.status in {"pending", "running"}:
            task.status, task.error, task.finished_at = (
                "cancelled",
                "group_source_unavailable",
                datetime.now(UTC),
            )
        clear_corpus(db, corpus)
        count += 1
    return count


def source_available(db: Session, corpus: StyleCorpus, user_id: UUID) -> bool:
    """Read-only guard, also safe inside the worker's account-first transaction."""
    if corpus.user_id != user_id or corpus.expires_at <= datetime.now(UTC):
        return False
    if corpus.source_group_id is None:
        return True
    revision = db.scalar(
        select(CollectedGroup.revision).where(CollectedGroup.id == corpus.source_group_id)
    )
    active = db.scalar(select(Account.active).where(Account.id == user_id))
    if not active or revision is None or corpus.source_revision != revision:
        return False
    return (
        _is_admin(db, user_id)
        or db.scalar(
            select(GroupGrant.user_id).where(
                GroupGrant.group_id == corpus.source_group_id, GroupGrant.user_id == user_id
            )
        )
        is not None
    )


def ingest_group_message(db: Session, event) -> bool:
    """Accept only explicitly enabled groups; caller commits. No account or reply side effects."""
    now = datetime.now(UTC)
    if (
        not isinstance(event.sent_at, datetime)
        or event.sent_at.tzinfo is None
        or event.sent_at < now - RETENTION
        or event.sent_at > now + timedelta(seconds=60)
        or event.kind not in {"text", "nontext"}
        or not isinstance(event.text, str)
        or not event.text.strip()
        or len(event.text.encode("utf-8")) > MAX_MESSAGE_BYTES
        or not 1 <= len(str(event.message_id)) <= 48
    ):
        return False
    group_id = db.scalar(
        select(CollectedGroup.id).where(
            CollectedGroup.bot_id == event.bot_id,
            CollectedGroup.qq_group_id == event.group_id,
            CollectedGroup.enabled.is_(True),
        )
    )
    if group_id is None:
        return False
    _group_lock(db, group_id)
    group = db.scalar(
        select(CollectedGroup)
        .where(CollectedGroup.id == group_id)
        .execution_options(populate_existing=True)
    )
    if (
        group is None
        or not group.enabled
        or (group.cleared_at and event.sent_at <= group.cleared_at)
    ):
        return False
    if db.scalar(
        select(GroupMessage.id).where(
            GroupMessage.group_id == group.id, GroupMessage.message_id == str(event.message_id)
        )
    ):
        return False
    group.last_ordinal += 1
    db.add(
        GroupMessage(
            group_id=group.id,
            message_id=str(event.message_id),
            qq_id=str(event.qq_id),
            name=" ".join(str(event.name).split())[:120],
            content=event.text,
            kind=event.kind,
            ordinal=group.last_ordinal,
            sent_at=event.sent_at,
        )
    )
    db.flush()
    return True


def purge_expired_messages(db: Session) -> int:
    # No group advisory locks here: never interleave locks from multiple groups
    # and accounts. Clear copies before source row deletion to preserve lock order.
    _invalidate_corpora(db, expired_only=True)
    result = db.execute(
        delete(GroupMessage).where(GroupMessage.sent_at < datetime.now(UTC) - RETENTION)
    )
    return result.rowcount


@router.get("")
def list_groups(user: GroupUser, db: DB):
    admin = _is_admin(db, user.id)
    query = select(CollectedGroup)
    if not admin:
        query = query.join(GroupGrant).where(GroupGrant.user_id == user.id)
    return {
        "is_admin": admin,
        "groups": [
            _group_view(db, group)
            for group in db.scalars(query.order_by(CollectedGroup.created_at, CollectedGroup.id))
        ],
    }


@router.post("", status_code=201)
def create_group(data: GroupInput, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    group_id = db.scalar(
        insert(CollectedGroup)
        .values(**data.model_dump())
        .on_conflict_do_nothing(index_elements=["bot_id", "qq_group_id"])
        .returning(CollectedGroup.id)
    )
    if group_id is None:
        raise HTTPException(409, "该机器人已登记此群，请在已有群上调整。")
    db.commit()
    return _group_view(db, db.get(CollectedGroup, group_id))


@router.patch("/{group_id}")
def update_group(group_id: UUID, data: GroupPatch, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    _group_lock(db, group_id)
    group = require_group(db, user.id, group_id)
    group.enabled = data.enabled
    if data.name is not None:
        if not data.name.strip():
            raise HTTPException(422, "群名称不能为空。")
        group.name = data.name.strip()
    db.commit()
    return _group_view(db, group)


@router.get("/{group_id}/grants")
def list_grants(group_id: UUID, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    require_group(db, user.id, group_id)
    rows = db.execute(
        select(Account.username, Account.id)
        .join(GroupGrant)
        .where(GroupGrant.group_id == group_id)
        .order_by(Account.username)
    )
    return {"grants": [{"username": name, "user_id": user_id} for name, user_id in rows]}


@router.put("/{group_id}/grants")
def grant_group(group_id: UUID, data: GrantInput, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    _group_lock(db, group_id)
    require_group(db, user.id, group_id)
    recipient_id = db.scalar(
        select(Account.id).where(
            Account.username == data.username.strip().lower(), Account.active.is_(True)
        )
    )
    if recipient_id is None:
        raise HTTPException(404, "未找到有效账号。")
    _locked_user(db, recipient_id)
    db.execute(
        insert(GroupGrant).values(group_id=group_id, user_id=recipient_id).on_conflict_do_nothing()
    )
    db.commit()
    return list_grants(group_id, user, db)


@router.delete("/{group_id}/grants/{user_id}")
def revoke_group(group_id: UUID, user_id: UUID, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    _group_lock(db, group_id)
    require_group(db, user.id, group_id)
    owner = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
    if owner is not None:
        _invalidate_corpora(db, group_id=group_id, user_id=user_id)
        db.execute(
            delete(GroupGrant).where(GroupGrant.group_id == group_id, GroupGrant.user_id == user_id)
        )
    db.commit()
    return list_grants(group_id, user, db)


@router.get("/{group_id}/members")
def list_members(group_id: UUID, user: GroupUser, db: DB):
    require_group(db, user.id, group_id)
    visible = _filters(group_id, *_bounds(None, None))
    latest = (
        select(
            GroupMessage.qq_id,
            GroupMessage.name,
            func.row_number()
            .over(
                partition_by=GroupMessage.qq_id,
                order_by=(GroupMessage.sent_at.desc(), GroupMessage.id.desc()),
            )
            .label("position"),
        )
        .where(*visible)
        .subquery()
    )
    rows = db.execute(
        select(GroupMessage.qq_id, latest.c.name, func.count())
        .join(latest, latest.c.qq_id == GroupMessage.qq_id)
        .where(*visible, latest.c.position == 1)
        .group_by(GroupMessage.qq_id, latest.c.name)
        .order_by(func.count().desc(), GroupMessage.qq_id)
    )
    return [{"qq_id": qq_id, "name": name, "message_count": count} for qq_id, name, count in rows]


@router.get("/{group_id}/messages")
def search_messages(
    group_id: UUID,
    user: GroupUser,
    db: DB,
    qq_id: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, max_length=200),
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    require_group(db, user.id, group_id)
    start, end = _bounds(start, end)
    filters = _filters(group_id, start, end, qq_id, keyword)
    total = db.scalar(select(func.count()).select_from(GroupMessage).where(*filters))
    rows = db.scalars(
        select(GroupMessage)
        .where(*filters)
        .order_by(GroupMessage.sent_at, GroupMessage.id)
        .offset(offset)
        .limit(50)
    )
    return {
        "total": total,
        "offset": offset,
        "limit": 50,
        "messages": [
            {
                "id": row.id,
                "qq_id": row.qq_id,
                "name": row.name,
                "content": row.content,
                "sent_at": row.sent_at,
                "kind": row.kind,
            }
            for row in rows
        ],
    }


@router.delete("/{group_id}/messages")
def clear_group(group_id: UUID, user: GroupUser, db: DB):
    _require_admin(db, user.id)
    _group_lock(db, group_id)
    group = require_group(db, user.id, group_id)
    cleared_corpora = _invalidate_corpora(db, group_id=group_id)
    group.revision += 1
    latest = db.scalar(
        select(func.max(GroupMessage.sent_at)).where(GroupMessage.group_id == group.id)
    )
    # Accepted clock skew must not let a just-cleared future-dated event replay.
    group.cleared_at = max(datetime.now(UTC), latest or datetime.now(UTC))
    removed = db.execute(delete(GroupMessage).where(GroupMessage.group_id == group.id)).rowcount
    db.commit()
    return {"removed_messages": removed, "cleared_corpora": cleared_corpora}


def _sample_windows(db: Session, group_id: UUID, data: CorpusQuery, settings):
    start, end = _bounds(data.start, data.end)
    filters = [
        *_filters(group_id, start, end, data.qq_id, data.keyword),
        GroupMessage.kind == "text",
    ]
    total = db.scalar(select(func.count()).select_from(GroupMessage).where(*filters))
    if not total:
        raise HTTPException(422, "所选成员与范围没有可用于蒸馏的文字消息。")
    # Rank only the chosen member's matching message metadata in PostgreSQL; do
    # not load the entire group's 30-day archive into application memory.
    max_anchors = min(
        settings.style_chat_max_windows, max(1, (settings.style_distillation_max_calls - 2) // 2)
    )
    anchor_count = min(total, max_anchors)
    before_limit, after_limit = (
        settings.style_chat_context_before,
        settings.style_chat_context_after,
    )
    ranks = (
        [1]
        if anchor_count == 1
        else sorted({1 + i * (total - 1) // (anchor_count - 1) for i in range(anchor_count)})
    )
    ranked = (
        select(
            GroupMessage.id,
            func.row_number()
            .over(order_by=(GroupMessage.sent_at, GroupMessage.id))
            .label("position"),
        )
        .where(*filters)
        .subquery()
    )
    anchors = list(
        db.scalars(
            select(GroupMessage)
            .join(ranked, ranked.c.id == GroupMessage.id)
            .where(ranked.c.position.in_(ranks))
            .order_by(GroupMessage.sent_at, GroupMessage.id)
        )
    )
    windows, selected_ids, selected_bytes = [], set(), 0
    for anchor in anchors:
        nearby = _filters(
            group_id, max(start, anchor.sent_at - WINDOW_GAP), min(end, anchor.sent_at + WINDOW_GAP)
        )
        cursor = tuple_(GroupMessage.sent_at, GroupMessage.id)
        position = (anchor.sent_at, anchor.id)
        before = list(
            db.scalars(
                select(GroupMessage)
                .where(*nearby, cursor < position)
                .order_by(GroupMessage.sent_at.desc(), GroupMessage.id.desc())
                .limit(before_limit)
            )
        )
        after = list(
            db.scalars(
                select(GroupMessage)
                .where(*nearby, cursor > position)
                .order_by(GroupMessage.sent_at, GroupMessage.id)
                .limit(after_limit)
            )
        )
        window = [*reversed(before), anchor, *after]
        for row in window:
            if row.id not in selected_ids:
                selected_ids.add(row.id)
                selected_bytes += len(
                    (f"{row.sent_at.isoformat()} {row.name}({row.qq_id})\n{row.content}\n").encode(
                        "utf-8"
                    )
                )
                if selected_bytes > settings.style_max_upload_bytes:
                    raise HTTPException(
                        422, "目标邻近消息超过临时材料上限，请缩小时间或关键词范围。"
                    )
        windows.append(window)
    rows = {row.id: row for window in windows for row in window}
    matched = db.scalar(
        select(func.count()).select_from(GroupMessage).where(*filters, GroupMessage.id.in_(rows))
    )
    return windows, {
        "total_target_messages": total,
        "matched_target_messages": total,
        "sampled_target_messages": matched,
        "sampled_anchor_messages": len(anchors),
        "selected_messages": len(rows),
        "max_anchors": max_anchors,
        "before": before_limit,
        "after": after_limit,
        "max_gap_seconds": 1800,
        "method": "group_target_windows_v1",
        "keyword_filtered": bool(data.keyword),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def _corpus_payload(windows, target_qq: str):
    # Union overlapping windows; disjoint windows retain separate conversations
    # and the real group ordinals, so missing context cannot become adjacency.
    parents = {row.id: row.id for window in windows for row in window}

    def root(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    for window in windows:
        for row in window:
            parents[root(row.id)] = root(window[0].id)
    rows = sorted(
        {row.id: row for window in windows for row in window}.values(),
        key=lambda row: (row.sent_at, row.id),
    )
    identities, people, segments, pieces = {}, {}, [], []
    offset = 0
    for row in rows:
        speaker = identities.setdefault(row.qq_id, f"0:p{len(identities)}")
        people.setdefault(
            speaker, {"id": speaker, "label": f"{row.name}({row.qq_id})", "messages": 0}
        )
        people[speaker]["messages"] += 1
        piece = f"{row.sent_at.isoformat()} {row.name}({row.qq_id})\n{row.content}\n"
        segments.append(
            {
                "id": f"0:{row.id}",
                "start": offset,
                "end": offset + len(piece),
                "text": piece,
                "speaker": speaker,
                "conversation": f"0:c{root(row.id)}",
                "ordinal": row.ordinal,
                "timestamp": row.sent_at.isoformat(),
                "kind": row.kind,
            }
        )
        pieces.append(piece)
        offset += len(piece)
    name = "授权群记录.txt"
    return (
        {
            "files": [
                {"name": name, "text": "".join(pieces), "encoding": "utf-8", "segments": segments}
            ]
        },
        list(people.values()),
        identities[target_qq],
        rows,
    )


@router.post("/{group_id}/corpus", status_code=201)
def create_corpus(group_id: UUID, data: CorpusQuery, request: Request, user: GroupUser, db: DB):
    _group_lock(db, group_id)
    user = _locked_user(db, user.id)
    group = require_group(db, user.id, group_id)
    pending = db.scalar(
        select(func.count())
        .select_from(StyleCorpus)
        .where(
            StyleCorpus.user_id == user.id,
            StyleCorpus.cleaned_at.is_(None),
            StyleCorpus.expires_at > datetime.now(UTC),
        )
    )
    if pending >= 4:
        raise HTTPException(409, "已有四份待处理材料，请先分析或放弃已有材料。")
    windows, sampling = _sample_windows(db, group_id, data, request.app.state.settings)
    payload, participants, target, rows = _corpus_payload(windows, data.qq_id)
    payload["group_sampling"] = sampling
    # Keep the same explicit upload bound even though the source came from SQL.
    size = sum(len(file["text"].encode("utf-8")) for file in payload["files"])
    if size > request.app.state.settings.style_max_upload_bytes:
        raise HTTPException(422, "目标邻近消息超过临时材料上限，请缩小时间或关键词范围。")
    file = payload["files"][0]
    preview = {
        "files": [
            {
                "name": file["name"],
                "chars": len(file["text"]),
                "encoding": "utf-8",
                "segments": len(file["segments"]),
                "unrecognized_lines": 0,
                "sections": [
                    {key: segment[key] for key in ("id", "start", "end", "speaker")}
                    | {"chars": len(segment["text"])}
                    for segment in file["segments"]
                ],
            }
        ],
        "participants": participants,
        "total_chars": len(file["text"]),
        "warnings": [
            "仅提取目标发言者的均匀采样与邻近消息，不代表全群所有材料。",
            "不连续窗口保持分开；含图片、语音或文件的消息只作为上下文，未解读其内容。",
        ],
        "group_sampling": sampling,
    }
    now = datetime.now(UTC)
    corpus = StyleCorpus(
        user_id=user.id,
        kind="personality",
        state="ready",
        payload=payload,
        preview=preview,
        source_group_id=group.id,
        source_revision=group.revision,
        expires_at=min(
            now + timedelta(hours=request.app.state.settings.style_corpus_ttl_hours),
            min(row.sent_at for row in rows) + RETENTION,
        ),
    )
    db.add(corpus)
    db.commit()
    return {"corpus": corpus_view(corpus), "target_speakers": [target]}
