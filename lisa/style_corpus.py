"""Temporary, bounded uploads. No source snippets are returned or logged."""

import base64
import binascii
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.models import Account, StyleCorpus, Task
from lisa.works import lock_account

router = APIRouter(tags=["style"])
StyleKind = Literal["personality", "writing"]
StyleUser = Annotated[Account, Depends(current_user)]
HEADER = re.compile(
    r"^(?:\[)?(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\])?\s+(?P<speaker>.+?)\s*$"
)
HEADER_REVERSE = re.compile(
    r"^(?P<speaker>.+?)\s+(?:\[)?\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\])?\s*$"
)
CHAPTER = re.compile(r"^(?:#{1,6}\s+.+|第[\d一二三四五六七八九十百千万零〇两]+[章节卷回部].*)$")


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1, max_length=12_000_000)


class UploadCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: StyleKind
    files: list[SourceFile] = Field(min_length=1, max_length=64)


def decode_source(data: bytes) -> tuple[str, str]:
    encodings = ["utf-8-sig", "gb18030"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    for encoding in encodings:
        try:
            value = data.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" in value or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            continue
        return value.replace("\r\n", "\n").replace("\r", "\n"), encoding
    raise HTTPException(422, "无法识别文本编码，请转换为 UTF-8 TXT/Markdown 后重新上传。")


def parse_chat(text: str, file_index: int) -> tuple[list[dict], list[dict], int]:
    segments, speakers = [], {}
    speaker_ids = {}
    current = None
    offset, unknown = 0, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = HEADER.match(stripped) or HEADER_REVERSE.match(stripped)
        if match:
            if current is not None:
                current["end"] = offset
                current["text"] = text[current["start"] : offset]
                segments.append(current)
            label = match["speaker"]
            # Deliberately scope IDs to the file, including exact nickname/account spelling.
            speaker = speaker_ids.setdefault(label, f"{file_index}:p{len(speaker_ids)}")
            speakers.setdefault(speaker, {"id": speaker, "label": label, "messages": 0})
            speakers[speaker]["messages"] += 1
            current = {"id": f"{file_index}:{len(segments)}", "start": offset, "speaker": speaker}
        elif current is None and stripped:
            unknown += 1
        offset += len(line)
    if current is not None:
        current["end"] = len(text)
        current["text"] = text[current["start"] :]
        segments.append(current)
    return segments, list(speakers.values()), unknown


def parse_writing(text: str, file_index: int) -> list[dict]:
    # Split by structural headings, retaining the preface and the whole source range.
    starts, offset = [0], 0
    for line in text.splitlines(keepends=True):
        if CHAPTER.match(line.strip()) and offset:
            starts.append(offset)
        offset += len(line)
    ends = starts[1:] + [len(text)]
    return [
        {
            "id": f"{file_index}:{i}",
            "start": start,
            "end": end,
            "text": text[start:end],
            "speaker": None,
        }
        for i, (start, end) in enumerate(zip(starts, ends))
        if text[start:end].strip()
    ]


def clear_corpus(db: Session, corpus: StyleCorpus) -> None:
    corpus.payload = {}
    preview = dict(corpus.preview)
    preview["participants"] = []
    preview["files"] = [
        {**item, "sections": [{**section, "speaker": None} for section in item.get("sections", [])]}
        for item in preview.get("files", [])
    ]
    corpus.preview = preview
    corpus.state = "cleaned"
    corpus.cleaned_at = datetime.now(UTC)


def clear_expired_corpora(db: Session) -> int:
    now = datetime.now(UTC)
    candidates = list(
        db.execute(
            select(StyleCorpus.id, StyleCorpus.user_id, StyleCorpus.task_id).where(
                StyleCorpus.cleaned_at.is_(None), StyleCorpus.expires_at <= now
            )
        )
    )
    cleaned = 0
    for corpus_id, user_id, task_id in candidates:
        # Match worker adoption order: account, task, corpus. Avoid waiting on an
        # account another sweep/worker currently owns.
        owner = db.scalar(
            select(Account).where(Account.id == user_id).with_for_update(skip_locked=True)
        )
        if owner is None:
            continue
        task = (
            db.scalar(select(Task).where(Task.id == task_id).with_for_update()) if task_id else None
        )
        corpus = db.scalar(
            select(StyleCorpus)
            .where(StyleCorpus.id == corpus_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if corpus is None or corpus.cleaned_at or corpus.expires_at > now:
            continue
        # Expiry also revokes a running job; its late result must not be adopted.
        if task and task.status in {"pending", "running"}:
            task.status, task.error, task.finished_at = "cancelled", "corpus_expired", now
        clear_corpus(db, corpus)
        cleaned += 1
    return cleaned


def require_corpus(db: Session, user_id: UUID, corpus_id: UUID, lock: bool = False) -> StyleCorpus:
    query = select(StyleCorpus).where(StyleCorpus.id == corpus_id, StyleCorpus.user_id == user_id)
    if lock:
        query = query.with_for_update()
    corpus = db.scalar(query.execution_options(populate_existing=True))
    if corpus is None:
        raise HTTPException(404, "上传材料不存在。")
    return corpus


def corpus_view(corpus: StyleCorpus) -> dict:
    return {
        "id": corpus.id,
        "kind": corpus.kind,
        "state": corpus.state,
        "cleanup_state": "cleaned" if corpus.cleaned_at else "pending",
        "preview": corpus.preview,
        "expires_at": corpus.expires_at,
        "cleaned_at": corpus.cleaned_at,
        "task_id": corpus.task_id,
    }


@router.post("/api/style/corpora", status_code=201)
def upload_corpus(body: UploadCorpus, request: Request, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    clear_expired_corpora(db)
    settings = request.app.state.settings
    max_files = getattr(settings, "style_max_files", 16)
    max_bytes = getattr(settings, "style_max_upload_bytes", 8_388_608)
    if len(body.files) > max_files:
        raise HTTPException(422, f"单次最多上传 {max_files} 个文件，请拆分任务。")
    files, previews, participants, warnings = [], [], [], []
    total_bytes = 0
    for position, source in enumerate(body.files):
        name = PurePath(source.name.replace("\\", "/")).name
        suffix = PurePath(name).suffix.lower()
        if suffix not in ({".txt"} if body.kind == "personality" else {".txt", ".md", ".markdown"}):
            raise HTTPException(422, "QQ 记录仅支持 TXT，作品支持 TXT、Markdown。")
        try:
            raw = base64.b64decode(source.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "上传内容损坏，请重新选择文件。") from None
        total_bytes += len(raw)
        if total_bytes > max_bytes:
            raise HTTPException(422, f"单次材料上限为 {max_bytes} 字节，请拆分或缩小范围。")
        text, encoding = decode_source(raw)
        if not text.strip():
            raise HTTPException(422, f"文件 {position + 1} 没有可分析文本。")
        if body.kind == "personality":
            segments, people, unknown = parse_chat(text, position)
            participants.extend(people)
            if not segments:
                warnings.append(f"文件 {position + 1} 未识别到 QQ 时间和发言者，请修正格式后重传。")
            if unknown:
                warnings.append(
                    f"文件 {position + 1} 有 {unknown} 行未归属发言者，分析不会静默使用这些行。"
                )
        else:
            segments, unknown = parse_writing(text, position), 0
        files.append({"name": name, "text": text, "encoding": encoding, "segments": segments})
        previews.append(
            {
                "name": name,
                "chars": len(text),
                "encoding": encoding,
                "segments": len(segments),
                "unrecognized_lines": unknown,
                "sections": [
                    {
                        "id": s["id"],
                        "start": s["start"],
                        "end": s["end"],
                        "chars": len(s["text"]),
                        "speaker": s["speaker"],
                    }
                    for s in segments
                ],
            }
        )
    corpus = StyleCorpus(
        user_id=user.id,
        kind=body.kind,
        payload={"files": files},
        preview={
            "files": previews,
            "participants": participants,
            "warnings": warnings,
            "total_chars": sum(p["chars"] for p in previews),
        },
        expires_at=datetime.now(UTC)
        + timedelta(hours=getattr(settings, "style_corpus_ttl_hours", 24)),
    )
    db.add(corpus)
    db.commit()
    return corpus_view(corpus)


@router.get("/api/style/corpora/{corpus_id}")
def get_corpus(corpus_id: UUID, user: StyleUser, db: DB):
    clear_expired_corpora(db)
    db.commit()
    return corpus_view(require_corpus(db, user.id, corpus_id))


@router.delete("/api/style/corpora/{corpus_id}")
def abandon_corpus(corpus_id: UUID, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    corpus = require_corpus(db, user.id, corpus_id)
    task = (
        db.scalar(select(Task).where(Task.id == corpus.task_id).with_for_update())
        if (corpus.task_id)
        else None
    )
    corpus = require_corpus(db, user.id, corpus_id, lock=True)
    if corpus.task_id:
        if task and task.status in {"pending", "running"}:
            task.status, task.error = "cancelled", "user_cancelled"
            task.finished_at = datetime.now(UTC)
    clear_corpus(db, corpus)
    db.commit()
    return corpus_view(corpus)
