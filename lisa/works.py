import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

from lisa.auth import DB, current_user
from lisa.life import LifeCandidate, adopt_life_events
from lisa.models import (
    Account,
    Chapter,
    ChapterVersion,
    ModelCall,
    Persona,
    Project,
    QQBinding,
    Task,
)
from lisa.worldbook import WorldbookEntry, validate_entries

router = APIRouter(tags=["works"])


def require_initialized(db: Session, user: Account | None) -> None:
    if (
        user is None
        or not user.active
        or not db.get(QQBinding, user.id)
        or not db.scalar(select(Persona.id).where(Persona.user_id == user.id))
    ):
        raise HTTPException(403, "请先完成 QQ 绑定与性格初始化。")


def initialized_user(user: Annotated[Account, Depends(current_user)], db: DB) -> Account:
    require_initialized(db, user)
    return user


User = Annotated[Account, Depends(initialized_user)]


def lock_account(db: Session, user_id: UUID) -> Account | None:
    return db.scalar(
        select(Account)
        .where(Account.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def require_project(db: Session, user: Account, project_id: UUID) -> Project:
    project = db.scalar(
        select(Project)
        .where(Project.id == project_id, Project.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise HTTPException(404, "作品不存在。")
    return project


def require_chapter(db: Session, user: Account, chapter_id: UUID) -> Chapter:
    chapter = db.scalar(
        select(Chapter)
        .join(Project)
        .where(Chapter.id == chapter_id, Project.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    if chapter is None:
        raise HTTPException(404, "章节不存在。")
    return chapter


def document_text(content: dict) -> str:
    kind = content.get("type")
    if kind == "text":
        return content.get("text", "")
    if kind == "hardBreak":
        return "\n"
    separator = (
        "\n" if kind in {"doc", "blockquote", "bulletList", "orderedList", "listItem"} else ""
    )
    return separator.join(document_text(node) for node in content.get("content", []))


def paragraph_document(paragraphs: list[str]) -> dict:
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": paragraph}]}
            if paragraph
            else {"type": "paragraph"}
            for paragraph in (paragraphs or [""])
        ],
    }


def invalidate_index(obj: Project | Chapter) -> None:
    """M08 must match this source revision before returning an indexed slice."""
    if isinstance(obj, Chapter):
        obj.version += 1
    else:
        obj.index_revision += 1


def _record_version(db: Session, chapter: Chapter, source: str, task_id: UUID | None = None):
    db.add(
        ChapterVersion(
            chapter_id=chapter.id,
            version=chapter.version,
            title=chapter.title,
            content=deepcopy(chapter.content),
            source=source,
            task_id=task_id,
        )
    )


def save_chapter(
    db: Session,
    chapter: Chapter,
    *,
    title: str,
    content: dict,
    source: str = "manual",
    task_id: UUID | None = None,
) -> Chapter:
    if chapter.title == title and chapter.content == content:
        return chapter
    chapter.title, chapter.content = title, deepcopy(content)
    invalidate_index(chapter)
    _record_version(db, chapter, source, task_id)
    return chapter


def _target_offset(content: dict, target: str) -> int:
    text = document_text(content)
    start = text.find(target)
    if not target.strip() or start < 0 or text.find(target, start + 1) >= 0:
        raise HTTPException(409, "重写片段不存在或出现多次，请从已保存正文重新选择唯一片段。")
    return start


def _slice_inline(nodes: list[dict], start: int, end: int) -> list[dict]:
    result, offset = [], 0
    for node in nodes:
        length = len(document_text(node))
        left, right = max(start - offset, 0), min(end - offset, length)
        if left < right:
            copied = deepcopy(node)
            if node["type"] == "text":
                copied["text"] = node["text"][left:right]
            result.append(copied)
        offset += length
    return result


def _fragment_range(content: dict, target: str) -> tuple[int, int, int, int]:
    start = _target_offset(content, target)
    end = start + len(target)
    blocks, offsets, offset = content["content"], [], 0
    for block in blocks:
        offsets.append((offset, offset + len(document_text(block))))
        offset = offsets[-1][1] + 1
    first = next(i for i, (_, stop) in enumerate(offsets) if start <= stop)
    last = next(i for i, (_, stop) in enumerate(offsets) if end <= stop)
    if any(block["type"] not in {"paragraph", "heading"} for block in blocks[first : last + 1]):
        raise HTTPException(409, "本阶段请在普通正文段落中选择重写片段。")
    return first, last, start - offsets[first][0], end - offsets[last][0]


def replace_fragment(content: dict, target: str, paragraphs: list[str]) -> dict:
    first, last, start, end = _fragment_range(content, target)
    blocks = content["content"]
    prefix = _slice_inline(blocks[first].get("content", []), 0, start)
    suffix = _slice_inline(blocks[last].get("content", []), end, len(document_text(blocks[last])))
    replacement = paragraph_document(paragraphs)["content"]
    replacement[0] = {
        **deepcopy(blocks[first]),
        "content": prefix + replacement[0].get("content", []),
    }
    replacement[-1]["content"] = replacement[-1].get("content", []) + suffix
    return {
        **deepcopy(content),
        "content": deepcopy(blocks[:first]) + replacement + deepcopy(blocks[last + 1 :]),
    }


def create_writing_task(
    db: Session,
    user: Account,
    project: Project,
    kind: str,
    goal: str,
    chapter_id: UUID | None = None,
    target_text: str | None = None,
    source: str = "web",
    notify_qq: bool = False,
) -> Task:
    from lisa.queue import enqueue_author

    user = lock_account(db, user.id)
    require_initialized(db, user)
    project = require_project(db, user, project.id)
    if kind not in {"scene", "rewrite"} or not goal.strip() or len(goal) > 4000:
        raise HTTPException(400, "请指定有限场景或局部重写及明确要求。")
    if kind == "scene":
        if project.completed:
            raise HTTPException(409, "作品已完结，请先明确继续创作，再新增场景。")
        if not project.agreement.strip():
            raise HTTPException(409, "请先保存作品的基本共识，再生成有限场景。")
        if chapter_id is not None or target_text is not None:
            raise HTTPException(400, "新场景不接受已有章节的重写范围。")
    else:
        if chapter_id is None or not target_text or len(target_text) > 4000:
            raise HTTPException(400, "请指定章节及不超过 4000 字的明确重写片段。")
        chapter = require_chapter(db, user, chapter_id)
        if chapter.project_id != project.id:
            raise HTTPException(404, "章节不属于此作品。")
        _fragment_range(chapter.content, target_text)
    if source == "autonomous" and (
        not project.is_current
        or project.completed
        or user.last_active_at is None
        or datetime.now(UTC) - user.last_active_at >= timedelta(hours=24)
    ):
        raise HTTPException(409, "当前不满足自主创作的启动条件。")
    task = Task(
        user_id=user.id,
        project_id=project.id,
        chapter_id=chapter_id,
        kind=kind,
        source=source,
        status="pending",
        request={
            "goal": goal.strip(),
            "target_text": target_text,
            "notify_qq": notify_qq if source != "web" else False,
        },
    )
    db.add(task)
    db.flush()
    task.queue_job_id = enqueue_author(db, task.id, user.id)
    return task


def adopt_result(db: Session, task: Task, result: dict) -> Chapter | None:
    user = lock_account(db, task.user_id)
    task = db.scalar(
        select(Task)
        .where(Task.id == task.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.active or task is None or task.status == "cancelled":
        return None
    if task.adopted_at:
        return db.get(Chapter, task.chapter_id)
    if task.status != "running":
        raise HTTPException(409, "此任务当前不能采用结果。")
    project = require_project(db, user, task.project_id)
    paragraphs = result["paragraphs"]
    if task.kind == "scene":
        position = (
            db.scalar(select(func.max(Chapter.position)).where(Chapter.project_id == project.id))
            or 0
        ) + 1
        chapter = Chapter(
            project_id=project.id,
            title=result["title"],
            position=position,
            content=paragraph_document(paragraphs),
            version=1,
        )
        db.add(chapter)
        db.flush()
        _record_version(db, chapter, "generated", task.id)
        task.chapter_id = chapter.id
    elif task.kind == "rewrite":
        chapter = require_chapter(db, user, task.chapter_id)
        if chapter.project_id != project.id:
            raise HTTPException(409, "任务的目标章节与作品不一致。")
        content = replace_fragment(chapter.content, task.request["target_text"], paragraphs)
        save_chapter(
            db, chapter, title=chapter.title, content=content, source="rewrite", task_id=task.id
        )
    else:
        raise HTTPException(409, "此任务不是正文生成任务。")
    if project.summary != result["summary"] or project.plot_state != result["plot_state"]:
        project.summary, project.plot_state = result["summary"], result["plot_state"]
        # These are fixed author context, not part of the project's derived search text.
    if project.kind == "diary":
        new_text = "\n".join(paragraphs)
        candidates = [LifeCandidate.model_validate(item) for item in result.get("life_events", [])]
        db.flush()
        adopt_life_events(
            db,
            user.id,
            [item for item in candidates if item.evidence in new_text],
            document_text(chapter.content),
            chapter=db.get(ChapterVersion, (chapter.id, chapter.version)),
        )
    task.adopted_at = task.finished_at = datetime.now(UTC)
    task.status, task.error = "completed", None
    db.flush()
    return chapter


class ProjectInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    kind: Literal["novel", "diary"] = "novel"

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class ProjectUpdate(BaseModel):
    title: str = Field(default="", min_length=1, max_length=160)
    worldbook: str = Field(default="", max_length=30000)
    worldbook_entries: list[WorldbookEntry] = Field(default_factory=list, max_length=200)
    constraints: str = Field(default="", max_length=12000)
    characters: str = Field(default="", max_length=30000)
    author_prompt: str = Field(default="", max_length=12000)
    summary: str = Field(default="", max_length=12000)
    plot_state: str = Field(default="", max_length=12000)
    agreement: str = Field(default="", max_length=12000)
    preferences: str = Field(default="", max_length=12000)
    tentative: str = Field(default="", max_length=12000)

    @field_validator("worldbook_entries")
    @classmethod
    def valid_entries(cls, value):
        return validate_entries(value)


class ProjectSummary(BaseModel):
    material_revision: str
    id: UUID
    title: str
    kind: str
    is_current: bool
    completed: bool
    index_revision: int


class ProjectView(ProjectUpdate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    is_current: bool
    completed: bool
    index_revision: int
    created_at: datetime


class ChapterInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    content: dict

    @field_validator("content")
    @classmethod
    def document(cls, value):
        allowed = {
            "doc",
            "paragraph",
            "text",
            "hardBreak",
            "heading",
            "bulletList",
            "orderedList",
            "listItem",
            "blockquote",
            "codeBlock",
            "horizontalRule",
        }

        def check(node):
            if not isinstance(node, dict) or node.get("type") not in allowed:
                raise ValueError("Invalid document node")
            if node["type"] == "text" and not isinstance(node.get("text"), str):
                raise ValueError("Invalid text node")
            if not isinstance(node.get("content", []), list):
                raise ValueError("Invalid document children")
            for child in node.get("content", []):
                check(child)

        check(value)
        if value["type"] != "doc":
            raise ValueError("Expected a Tiptap document")
        return value


class ChapterSummary(BaseModel):
    id: UUID
    title: str
    position: int
    version: int


class ChapterView(ChapterInput):
    id: UUID
    project_id: UUID
    position: int
    version: int
    text: str


def chapter_view(chapter: Chapter) -> ChapterView:
    return ChapterView(
        id=chapter.id,
        project_id=chapter.project_id,
        title=chapter.title,
        position=chapter.position,
        content=chapter.content,
        version=chapter.version,
        text=document_text(chapter.content),
    )


class TaskInput(BaseModel):
    kind: Literal["scene", "rewrite"]
    goal: str = Field(min_length=1, max_length=4000)
    chapter_id: UUID | None = None
    target_text: str | None = Field(default=None, max_length=4000)


class TaskView(BaseModel):
    id: UUID
    project_id: UUID | None
    chapter_id: UUID | None
    kind: str
    source: str
    status: str
    goal: str
    target_text: str | None
    retry_of: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    adopted_at: datetime | None
    error: str | None
    can_cancel: bool
    can_retry: bool
    input_tokens: int
    output_tokens: int
    charged_usd: Decimal
    reserved_usd: Decimal


class FinishingTaskView(TaskView):
    project_title: str


class HandoffView(BaseModel):
    finishing_tasks: list[FinishingTaskView]


def task_view(db: Session, task: Task, *, calls=None) -> TaskView:
    if calls is None:
        calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task.id)).all()
    unresolved = any(call.state in {"reserved", "unknown"} for call in calls)
    return TaskView(
        **{
            name: getattr(task, name)
            for name in (
                "id",
                "project_id",
                "chapter_id",
                "kind",
                "source",
                "status",
                "retry_of",
                "created_at",
                "started_at",
                "finished_at",
                "adopted_at",
                "error",
            )
        },
        goal=task.request.get("goal", ""),
        target_text=task.request.get("target_text"),
        can_cancel=task.status in {"pending", "running"} and not task.adopted_at,
        can_retry=task.status == "failed"
        and not task.adopted_at
        and not unresolved
        and task.error != "model_unknown",
        input_tokens=sum(call.input_tokens or 0 for call in calls),
        output_tokens=sum(call.output_tokens or 0 for call in calls),
        charged_usd=sum((call.charged_usd or Decimal(0) for call in calls), Decimal(0)),
        reserved_usd=sum(
            (call.reserved_usd for call in calls if call.state == "reserved"), Decimal(0)
        ),
    )


def task_views(db: Session, tasks: list[Task]) -> list[TaskView]:
    """Load the ledger once for a list, including tasks without any calls."""
    calls_by_task = defaultdict(list)
    if tasks:
        for call in db.scalars(select(ModelCall).where(ModelCall.task_id.in_(
            [task.id for task in tasks]
        ))):
            calls_by_task[call.task_id].append(call)
    return [task_view(db, task, calls=calls_by_task[task.id]) for task in tasks]


@router.get("/api/projects", response_model=list[ProjectView | ProjectSummary])
def list_projects(user: User, db: DB, summary: bool = False):
    if summary:
        rows = db.execute(select(
            Project.id, Project.title, Project.kind, Project.is_current,
            Project.completed, Project.index_revision,
            # Search revision deliberately excludes fixed author context. This read-only
            # fingerprint covers every material field without invalidating search indexes.
            func.md5(cast(func.jsonb_build_array(*[
                getattr(Project, name) for name in ProjectUpdate.model_fields
            ]), Text)).label("material_revision"),
        ).where(Project.user_id == user.id).order_by(Project.created_at)).mappings()
        return [ProjectSummary(**row) for row in rows]
    return [ProjectView.model_validate(item) for item in db.scalars(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at)
    )]


@router.post("/api/projects", response_model=ProjectView, status_code=201)
def create_project(body: ProjectInput, user: User, db: DB):
    from lisa.scheduling import handle_project_change

    require_initialized(db, lock_account(db, user.id))
    _clear_current_project(db, user.id)
    project = Project(user_id=user.id, title=body.title, kind=body.kind)
    db.add(project)
    db.flush()
    handle_project_change(db, user.id)
    db.commit()
    return project


def _clear_current_project(db: Session, user_id: UUID) -> None:
    for project in db.scalars(
        select(Project).where(Project.user_id == user_id, Project.is_current)
    ):
        project.is_current = False
    # Flush the old flag before setting a new one (one-current-project unique index).
    db.flush()


@router.get("/api/projects/handoff", response_model=HandoffView)
def get_handoff(user: User, db: DB):
    rows = db.execute(
        select(Task, Project.title)
        .join(Project, Project.id == Task.project_id)
        .where(
            Task.user_id == user.id,
            Project.user_id == user.id,
            Project.is_current.is_(False),
            Task.kind.in_(["scene", "rewrite"]),
            Task.status.in_(["pending", "running"]),
            Task.started_at.is_not(None),
        )
        .order_by(Task.created_at, Task.id)
    ).all()
    return HandoffView(
        finishing_tasks=[
            FinishingTaskView(**view.model_dump(), project_title=title)
            for view, (_, title) in zip(task_views(db, [task for task, _ in rows]), rows)
        ]
    )


@router.post("/api/projects/{project_id}/activate", response_model=ProjectView)
def activate_project(project_id: UUID, user: User, db: DB):
    from lisa.scheduling import handle_project_change

    require_initialized(db, lock_account(db, user.id))
    project = require_project(db, user, project_id)
    if not project.is_current:
        _clear_current_project(db, user.id)
        project.is_current = True
    project.completed = False
    db.flush()
    handle_project_change(db, user.id)
    db.commit()
    return project


@router.post("/api/projects/{project_id}/complete", response_model=ProjectView)
def complete_project(project_id: UUID, user: User, db: DB):
    from lisa.scheduling import handle_project_change

    require_initialized(db, lock_account(db, user.id))
    project = require_project(db, user, project_id)
    project.completed = True
    db.flush()
    handle_project_change(db, user.id)
    db.commit()
    return project


@router.get("/api/projects/{project_id}", response_model=ProjectView)
def get_project(project_id: UUID, user: User, db: DB):
    return require_project(db, user, project_id)


@router.put("/api/projects/{project_id}", response_model=ProjectView)
def update_project(project_id: UUID, body: ProjectUpdate, user: User, db: DB):
    require_initialized(db, lock_account(db, user.id))
    project = require_project(db, user, project_id)
    changes = {
        key: value
        for key, value in body.model_dump(exclude_unset=True).items()
        if getattr(project, key) != value
    }
    if changes:
        for key, value in changes.items():
            setattr(project, key, value)
        if changes.keys() & {
            "title",
            "worldbook",
            "worldbook_entries",
            "characters",
            "preferences",
            "tentative",
        }:
            invalidate_index(project)
        db.commit()
    return project


@router.get(
    "/api/projects/{project_id}/chapters", response_model=list[ChapterView | ChapterSummary]
)
def list_chapters(project_id: UUID, user: User, db: DB, summary: bool = False):
    if summary:
        if db.scalar(select(Project.id).where(
            Project.id == project_id, Project.user_id == user.id
        )) is None:
            raise HTTPException(404, "作品不存在。")
        rows = db.execute(select(
            Chapter.id, Chapter.title, Chapter.position, Chapter.version,
        ).where(Chapter.project_id == project_id).order_by(Chapter.position)).mappings()
        return [ChapterSummary(**row) for row in rows]
    project = require_project(db, user, project_id)
    return [
        chapter_view(chapter)
        for chapter in db.scalars(
            select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)
        )
    ]


@router.get("/api/chapters/{chapter_id}", response_model=ChapterView)
def get_chapter(chapter_id: UUID, user: User, db: DB):
    return chapter_view(require_chapter(db, user, chapter_id))


@router.put("/api/chapters/{chapter_id}", response_model=ChapterView)
def update_chapter(chapter_id: UUID, body: ChapterInput, user: User, db: DB):
    require_initialized(db, lock_account(db, user.id))
    chapter = require_chapter(db, user, chapter_id)
    save_chapter(db, chapter, title=body.title, content=body.content)
    db.commit()
    return chapter_view(chapter)


@router.post("/api/projects/{project_id}/tasks", response_model=TaskView, status_code=201)
def new_task(project_id: UUID, body: TaskInput, user: User, db: DB):
    project = require_project(db, user, project_id)
    task = create_writing_task(db, user, project, **body.model_dump())
    db.commit()
    return task_view(db, task)


class VersionInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    title: str
    source: str
    created_at: datetime


class VersionDetail(VersionInfo):
    content: dict
    text: str


@router.get("/api/chapters/{chapter_id}/versions", response_model=list[VersionInfo])
def list_versions(chapter_id: UUID, user: User, db: DB):
    chapter = require_chapter(db, user, chapter_id)
    return db.scalars(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.version.desc())
    ).all()


def _require_version(db: Session, chapter_id: UUID, version: int) -> ChapterVersion:
    saved = db.get(ChapterVersion, (chapter_id, version))
    if saved is None:
        raise HTTPException(404, "正文版本不存在。")
    return saved


@router.get("/api/chapters/{chapter_id}/versions/{version}", response_model=VersionDetail)
def get_version(chapter_id: UUID, version: int, user: User, db: DB):
    chapter = require_chapter(db, user, chapter_id)
    saved = _require_version(db, chapter.id, version)
    return VersionDetail(
        **VersionInfo.model_validate(saved).model_dump(),
        content=saved.content,
        text=document_text(saved.content),
    )


@router.post("/api/chapters/{chapter_id}/versions/{version}/restore", response_model=ChapterView)
def restore_version(chapter_id: UUID, version: int, user: User, db: DB):
    require_initialized(db, lock_account(db, user.id))
    chapter = require_chapter(db, user, chapter_id)
    saved = _require_version(db, chapter.id, version)
    save_chapter(
        db, chapter, title=saved.title, content=saved.content, source=f"restore:{saved.version}"
    )
    db.commit()
    return chapter_view(chapter)


@router.get("/api/projects/{project_id}/export", response_class=PlainTextResponse)
def export_project(project_id: UUID, user: User, db: DB):
    project = require_project(db, user, project_id)
    chapters = db.scalars(
        select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)
    ).all()
    if not chapters:
        raise HTTPException(409, "这部作品还没有已保存的正文。")
    # A fixed prefix also avoids Windows reserved device names such as CON.txt.
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", project.title).strip(" .")[:120]
    filename = quote(f"LISA-{title or '作品正文'}.txt", safe="")
    text = (
        "\n\n".join(
            [
                project.title,
                *(f"{chapter.title}\n\n{document_text(chapter.content)}" for chapter in chapters),
            ]
        )
        + "\n"
    )
    return PlainTextResponse(
        text, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    )


@router.get("/api/projects/{project_id}/tasks", response_model=list[TaskView])
def list_tasks(project_id: UUID, user: User, db: DB):
    project = require_project(db, user, project_id)
    tasks = db.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.kind.in_(["scene", "rewrite"]))
        .order_by(Task.created_at.desc())
        .limit(50)
    )
    return task_views(db, list(tasks))


def require_writing_task(
    db: Session, user: Account, task_id: UUID, *, for_update: bool = False
) -> Task:
    query = (
        select(Task)
        .join(Project)
        .where(
            Task.id == task_id,
            Task.user_id == user.id,
            Project.user_id == user.id,
            Task.kind.in_(["scene", "rewrite"]),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update(of=Task)
    task = db.scalar(query)
    if task is None:
        raise HTTPException(404, "任务不存在。")
    return task


@router.post("/api/tasks/{task_id}/cancel", response_model=TaskView)
def cancel_task(task_id: UUID, user: User, db: DB):
    require_initialized(db, lock_account(db, user.id))
    task = require_writing_task(db, user, task_id, for_update=True)
    if task.status in {"pending", "running"} and task.adopted_at is None:
        task.status, task.error, task.finished_at = "cancelled", "user_cancelled", datetime.now(UTC)
        db.commit()
    return task_view(db, task)


@router.post("/api/tasks/{task_id}/retry", response_model=TaskView, status_code=201)
def retry_task(task_id: UUID, user: User, db: DB):
    require_initialized(db, lock_account(db, user.id))
    original = require_writing_task(db, user, task_id, for_update=True)
    if not task_view(db, original).can_retry:
        raise HTTPException(409, "仅最终失败且结果明确的生成任务可以手动重试。")
    task = create_writing_task(
        db,
        user,
        require_project(db, user, original.project_id),
        original.kind,
        original.request["goal"],
        chapter_id=original.chapter_id,
        target_text=original.request.get("target_text"),
        source="web",
        notify_qq=False,
    )
    task.retry_of = original.id
    db.commit()
    return task_view(db, task)
