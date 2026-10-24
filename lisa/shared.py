"""Small deployment-wide library: every signed-in user can contribute and correct."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from lisa.auth import DB, current_user
from lisa.models import (
    Account,
    IndexState,
    OutboundMessage,
    SharedAsset,
    SharedEntry,
    SharedMute,
    SharedPreference,
    SharedRevision,
    Task,
)
from lisa.works import lock_account

router = APIRouter(prefix="/api/shared", tags=["shared"])
User = Annotated[Account, Depends(current_user)]
MAX_ASSET_BYTES = 4 * 1024 * 1024
FIELDS = (
    "kind",
    "title",
    "content",
    "aliases",
    "tags",
    "source_url",
    "source_note",
    "origin",
    "asset_id",
    "status",
    "allow_chat",
    "allow_writing",
    "occurred_at",
    "source_time",
    "expires_at",
)
Short = Annotated[str, Field(min_length=1, max_length=80)]


class EntryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["meme"]
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=2400)
    aliases: list[Short] = Field(default_factory=list, max_length=16)
    tags: list[Short] = Field(default_factory=list, max_length=12)
    source_url: str = Field(default="", max_length=2000)
    source_note: str = Field(default="", max_length=800)
    origin: Literal["public", "contribution"] = "contribution"
    asset_id: UUID | None = None
    status: Literal["candidate", "active", "archived", "withdrawn"] = "active"
    allow_chat: bool = True
    allow_writing: bool = False
    occurred_at: AwareDatetime | None = None
    source_time: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    contribute: Literal[True]
    revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_source(self):
        if self.source_url:
            value = urlsplit(self.source_url)
            if (
                value.scheme not in {"https", "http"}
                or not value.hostname
                or value.username is not None
                or value.password is not None
            ):
                raise ValueError("来源须为公开 HTTP(S) 链接")
        if self.origin == "public" and not self.source_url:
            raise ValueError("公开资料须填写独立公开来源")
        if self.kind != "meme" and self.asset_id:
            raise ValueError("只有表情条目可关联图片")
        return self


class VersionBody(BaseModel):
    revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


class PreferenceBody(BaseModel):
    meme_mode: Literal["normal", "reduced", "off"]


class MuteBody(BaseModel):
    muted: bool


def _lock_user(db, user_id):
    account = lock_account(db, user_id)
    if account is None or not account.active:
        raise HTTPException(401, "账号已退出或删除，请重新登录。")
    return account


def entry_data(item):
    result = {}
    for name in FIELDS:
        value = getattr(item, name)
        result[name] = (
            value.isoformat()
            if isinstance(value, datetime)
            else (str(value) if isinstance(value, UUID) else value)
        )
    return result


def entry_view(db, item, user_id):
    return {
        "id": str(item.id),
        **entry_data(item),
        "revision": item.revision,
        "editor_id": item.editor_id,
        "updated_at": item.updated_at,
        "human_edited": item.human_edited,
        "muted": db.get(SharedMute, (user_id, item.id)) is not None,
    }


def require_entry(db, entry_id, revision=None):
    item = db.scalar(select(SharedEntry).where(SharedEntry.id == entry_id).with_for_update())
    if item is None or item.kind != "meme":
        raise HTTPException(404, "共有条目已不存在。")
    if revision is not None and item.revision != revision:
        raise HTTPException(409, "条目已被更新，请重新载入后再保存。当前输入仍保留。")
    return item


def save_revision(db, item, *, editor_id=None, human=False, initial=False):
    if not initial:
        item.revision += 1
    item.editor_id = editor_id
    item.human_edited = item.human_edited or human
    item.updated_at = datetime.now(UTC)
    db.flush()
    data = entry_data(item)
    data["private_owner"] = str(item.contributor_id) if item.origin == "contribution" else None
    db.add(SharedRevision(entry_id=item.id, revision=item.revision, editor_id=editor_id, data=data))
    # Readers join source revision; deleting derived slices also avoids retaining withdrawn text.
    db.execute(
        delete(IndexState).where(
            IndexState.source_kind == "shared", IndexState.source_id == item.id
        )
    )
    db.flush()


@router.get("")
def list_entries(
    db: DB,
    user: User,
    query: str = "",
    kind: str = "",
    status: str = "active",
    offset: int = 0,
    limit: int = 30,
):
    statement = select(SharedEntry).where(SharedEntry.kind == "meme")
    if kind:
        statement = statement.where(SharedEntry.kind == kind)
    if status:
        statement = statement.where(SharedEntry.status == status)
    if query:
        pattern = "%" + query[:160].replace("%", "\\%").replace("_", "\\_") + "%"
        statement = statement.where(
            or_(SharedEntry.title.ilike(pattern), SharedEntry.content.ilike(pattern))
        )
    items = db.scalars(
        statement.order_by(SharedEntry.updated_at.desc(), SharedEntry.id)
        .offset(max(0, offset))
        .limit(min(50, max(1, limit)))
    )
    return [entry_view(db, item, user.id) for item in items]


@router.get("/preferences")
def preferences(db: DB, user: User):
    item = db.get(SharedPreference, user.id)
    return {"meme_mode": item.meme_mode if item else "normal"}


@router.put("/preferences")
def save_preferences(body: PreferenceBody, db: DB, user: User):
    _lock_user(db, user.id)
    db.execute(
        insert(SharedPreference)
        .values(user_id=user.id, meme_mode=body.meme_mode)
        .on_conflict_do_update(index_elements=["user_id"], set_={"meme_mode": body.meme_mode})
    )
    db.commit()
    return body.model_dump()


@router.post("/assets", status_code=201)
async def upload_asset(request: Request, db: DB, user: User):
    if request.headers.get("X-LISA-Contribute") != "1":
        raise HTTPException(422, "请明确同意将图片贡献给本部署的所有用户。")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_ASSET_BYTES:
            raise HTTPException(413, "图片最多 4 MiB。")
    content = bytes(body)
    mime = (
        "image/png"
        if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 32
        else "image/jpeg"
        if content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
        else "image/gif"
        if content.startswith((b"GIF87a", b"GIF89a")) and len(content) > 14
        else "image/webp"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        else None
    )
    if mime is None:
        raise HTTPException(422, "请选择有效的 PNG、JPEG、GIF 或 WebP 图片。")
    _lock_user(db, user.id)
    digest = hashlib.sha256(content).hexdigest()
    db.execute(
        insert(SharedAsset)
        .values(digest=digest, mime=mime, data=content, uploader_id=user.id)
        .on_conflict_do_nothing(index_elements=["digest"])
    )
    item = db.scalar(select(SharedAsset).where(SharedAsset.digest == digest))
    db.commit()
    return {"id": item.id, "mime": item.mime}


def asset_available(db, asset_id, user_id):
    item = db.get(SharedAsset, asset_id)
    # Upload itself requires explicit deployment-wide contribution consent.
    if item is None:
        raise HTTPException(404, "表情素材不可用。")
    return item


@router.get("/assets/{asset_id}")
def read_asset(asset_id: UUID, db: DB, user: User):
    item = asset_available(db, asset_id, user.id)
    return Response(item.data, media_type=item.mime, headers={"X-Content-Type-Options": "nosniff"})


def apply_body(db, item, body, user):
    if body.asset_id:
        asset_available(db, body.asset_id, user.id)
    for name in FIELDS:
        setattr(item, name, getattr(body, name))
    if item.origin == "public":
        item.contributor_id = None


@router.post("", status_code=201)
def create_entry(body: EntryBody, db: DB, user: User):
    _lock_user(db, user.id)
    item = SharedEntry(contributor_id=user.id, revision=1)
    apply_body(db, item, body, user)
    db.add(item)
    save_revision(db, item, editor_id=user.id, human=True, initial=True)
    db.commit()
    return entry_view(db, item, user.id)


@router.put("/{entry_id}")
def edit_entry(entry_id: UUID, body: EntryBody, db: DB, user: User):
    _lock_user(db, user.id)
    if body.revision is None:
        raise HTTPException(422, "保存需要当前版本。")
    item = require_entry(db, entry_id, body.revision)
    # Compare typed values: UTC '+00:00' and JSON 'Z' are the same instant.
    # A no-op must not freeze an automatic source as a human correction.
    if any(getattr(item, name) != getattr(body, name) for name in FIELDS):
        if body.origin == "contribution" and item.origin == "public":
            item.contributor_id = user.id
        apply_body(db, item, body, user)
        save_revision(db, item, editor_id=user.id, human=True)
    db.commit()
    return entry_view(db, item, user.id)


@router.get("/{entry_id}/versions")
def versions(entry_id: UUID, db: DB, user: User):
    require_entry(db, entry_id)
    return [
        {
            "revision": v.revision,
            "editor_id": v.editor_id,
            "created_at": v.created_at,
            "data": {k: val for k, val in v.data.items() if k in FIELDS},
        }
        for v in db.scalars(
            select(SharedRevision)
            .where(SharedRevision.entry_id == entry_id)
            .order_by(SharedRevision.revision.desc())
            .limit(50)
        )
    ]


@router.post("/{entry_id}/restore")
def restore_entry(entry_id: UUID, body: VersionBody, db: DB, user: User):
    _lock_user(db, user.id)
    item = require_entry(db, entry_id, body.revision)
    version = db.get(SharedRevision, (entry_id, body.target_revision))
    if version is None:
        raise HTTPException(404, "该版本已不存在或其私人来源已清理。")
    data = {k: v for k, v in version.data.items() if k in FIELDS}
    restored = EntryBody(**data, contribute=True)
    private_owner = version.data.get("private_owner")
    if private_owner and not db.get(Account, UUID(private_owner)):
        raise HTTPException(404, "该版本的私人来源已清理。")
    item.contributor_id = UUID(private_owner) if private_owner else None
    apply_body(db, item, restored, user)
    save_revision(db, item, editor_id=user.id, human=True)
    db.commit()
    return entry_view(db, item, user.id)


@router.put("/{entry_id}/mute")
def mute_entry(entry_id: UUID, body: MuteBody, db: DB, user: User):
    _lock_user(db, user.id)
    require_entry(db, entry_id)
    if body.muted:
        db.execute(
            insert(SharedMute).values(user_id=user.id, entry_id=entry_id).on_conflict_do_nothing()
        )
    else:
        db.execute(
            delete(SharedMute).where(SharedMute.user_id == user.id, SharedMute.entry_id == entry_id)
        )
    db.commit()
    return {"muted": body.muted}


@router.post("/{entry_id}/analyze", status_code=202)
def analyze_entry(entry_id: UUID, request: Request, db: DB, user: User):
    from lisa.shared_jobs import enqueue_shared

    _lock_user(db, user.id)
    item = require_entry(db, entry_id)
    if item.status == "withdrawn":
        raise HTTPException(409, "请先明确恢复该条目。")
    task = enqueue_shared(
        db,
        request.app.state.settings,
        "shared_analyze",
        {
            "entry_id": str(item.id),
            "revision": item.revision,
        },
    )
    db.commit()
    return {"task_id": task.id, "status": task.status}


def cleanup_account_contributions(db, user_id):
    """Called inside account deletion; public knowledge has no private source copy."""
    private_ids = list(
        db.scalars(
            select(SharedEntry.id).where(
                SharedEntry.contributor_id == user_id, SharedEntry.origin == "contribution"
            )
        )
    )
    old_private = list(
        db.scalars(
            select(SharedRevision).where(
                SharedRevision.data["private_owner"].astext == str(user_id)
            )
        )
    )
    for version in old_private:
        db.delete(version)
    if private_ids:
        db.execute(
            update(Task)
            .where(
                Task.user_id.is_(None),
                Task.request["entry_id"].astext.in_([str(i) for i in private_ids]),
            )
            .values(status="cancelled", request={}, input_snapshot=None, result=None)
        )
        db.execute(
            delete(IndexState).where(
                IndexState.source_kind == "shared", IndexState.source_id.in_(private_ids)
            )
        )
        db.execute(
            update(OutboundMessage)
            .where(OutboundMessage.meme_id.in_(private_ids))
            .values(meme_id=None, meme_revision=None)
        )
        db.execute(delete(SharedEntry).where(SharedEntry.id.in_(private_ids)))
    db.flush()
    # Keep a deduplicated file only while another explicit contribution needs it.
    db.execute(
        delete(SharedAsset).where(
            SharedAsset.uploader_id == user_id,
            ~exists().where(SharedEntry.asset_id == SharedAsset.id),
        )
    )


@router.get("/jobs/{task_id}")
def shared_job(task_id: UUID, db: DB, user: User):
    task = db.get(Task, task_id)
    if (
        task is None
        or task.user_id is not None
        or task.kind not in {"shared_analyze", "shared_index"}
    ):
        raise HTTPException(404, "共有维护任务不存在。")
    return {"task_id": task.id, "status": task.status, "error": task.error, "result": task.result}
