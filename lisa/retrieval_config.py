"""Author-only optional recall settings and live, metadata-backed references."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.auth import DB
from lisa.models import Project
from lisa.retrieval import _source, indexing_enabled, load_task_references
from lisa.works import (
    User,
    lock_account,
    require_initialized,
    require_project,
    require_writing_task,
)

router = APIRouter(tags=["retrieval"])


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[Literal["memory", "project", "chapter"]] = Field(
        default_factory=lambda: ["memory", "project", "chapter"], max_length=3
    )
    limit: int = Field(default=5, ge=1, le=12, strict=True)
    max_chars: int = Field(default=1500, ge=300, le=6000, strict=True)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("资料范围不能重复。")
        return value


def author_retrieval_config(db: Session, user_id: UUID, project_id: UUID) -> RetrievalConfig:
    value = db.scalar(
        select(Project.retrieval_config).where(Project.id == project_id, Project.user_id == user_id)
    )
    return RetrievalConfig.model_validate(value if value is not None else {})


def _view(project: Project, request: Request) -> dict:
    return {
        "config": RetrievalConfig.model_validate(project.retrieval_config),
        "defaults": RetrievalConfig(),
        "enabled": indexing_enabled(request.app.state.settings),
    }


@router.get("/api/projects/{project_id}/retrieval")
def get_config(project_id: UUID, request: Request, user: User, db: DB):
    return _view(require_project(db, user, project_id), request)


@router.put("/api/projects/{project_id}/retrieval")
def update_config(project_id: UUID, data: RetrievalConfig, request: Request, user: User, db: DB):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    project = require_project(db, user, project_id)
    project.retrieval_config = data.model_dump()
    db.commit()
    return _view(project, request)


@router.get("/api/tasks/{task_id}/references")
def task_references(task_id: UUID, user: User, db: DB):
    task = require_writing_task(db, user, task_id)
    items = []
    for position, ref in enumerate(load_task_references(db, task.id)):
        source = _source(
            db,
            ref["source_kind"],
            UUID(ref["source_id"]),
            user.id,
            task.project_id,
            "role" if ref["source_kind"] == "character" else "writing",
            UUID(ref["source_id"]) if ref["source_kind"] == "character" else None,
        )
        state = (
            "unavailable"
            if source is None
            else "changed"
            if source.revision != ref["revision"]
            else "available"
        )
        title = source.metadata.get("title", "长期记忆") if state == "available" else None
        if state == "available" and source.kind == "project":
            section = next(
                (name for name, start, end in source.sections if start <= ref["start"] < end), ""
            )
            if section.startswith("worldbook_entry:"):
                title = next(
                    item["title"]
                    for item in source.metadata["worldbook_entries"]
                    if item["id"] == section.split(":", 1)[1]
                )
        items.append(
            {
                "position": position,
                "source_kind": ref["source_kind"],
                "revision": ref["revision"],
                "start": ref["start"],
                "end": ref["end"],
                "state": state,
                "title": title,
                "text": source.text[ref["start"] : ref["end"]] if state == "available" else None,
            }
        )
    return {"references": items}
