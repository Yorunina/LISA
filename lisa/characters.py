"""Project-owned character configuration and participation metadata."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lisa.auth import DB
from lisa.models import Character, CharacterParticipation, IndexState, Project, Task
from lisa.works import (
    User,
    lock_account,
    require_initialized,
    require_project,
    require_writing_task,
)

router = APIRouter(tags=["characters"])
DEFAULT_CHARACTER_PROMPT = (
    "只扮演这个角色，根据提供的已知信息提出自身的对话、行动和反应。"
    "不知道的信息保持未知，不补全未知秘密，不决定全局剧情，不创建其他角色或 Agent。"
)


class KnowledgeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=500)
    kind: Literal["fact", "inference"]


class CharacterRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    limit: int = Field(default=3, ge=1, le=8, strict=True)
    max_chars: int = Field(default=1500, ge=300, le=3000, strict=True)


class CharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    prompt: str = Field(min_length=1, max_length=3000)
    known_information: str = Field(default="", max_length=6000)
    current_scene: str = Field(default="", max_length=1500)
    retrieval_config: CharacterRetrievalConfig = Field(default_factory=CharacterRetrievalConfig)
    enabled: bool = False

    @model_validator(mode="after")
    def enabled_needs_knowledge(self):
        if self.enabled and not self.known_information:
            raise ValueError("启用角色前，请填写该角色确实知道的信息。")
        return self


class CharacterView(CharacterInput):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    revision: int
    created_at: datetime


def _require_character(db: Session, user_id: UUID, character_id: UUID) -> Character:
    character = db.scalar(
        select(Character)
        .join(Project)
        .where(Character.id == character_id, Project.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if character is None:
        raise HTTPException(404, "角色不存在。")
    return character


def _require_unique_name(db: Session, project_id: UUID, name: str, current_id=None):
    existing = db.scalar(
        select(Character.id).where(Character.project_id == project_id, Character.name == name)
    )
    if existing is not None and existing != current_id:
        raise HTTPException(409, "这部作品已有同名角色。")


@router.get("/api/projects/{project_id}/characters", response_model=list[CharacterView])
def list_characters(project_id: UUID, user: User, db: DB):
    require_project(db, user, project_id)
    return db.scalars(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at, Character.id)
    ).all()


@router.post("/api/projects/{project_id}/characters", response_model=CharacterView, status_code=201)
def create_character(project_id: UUID, data: CharacterInput, user: User, db: DB):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    require_project(db, user, project_id)
    _require_unique_name(db, project_id, data.name)
    character = Character(project_id=project_id, **data.model_dump())
    db.add(character)
    db.commit()
    return character


@router.get("/api/characters/defaults")
def character_defaults(request: Request, user: User):
    from lisa.retrieval import indexing_enabled

    return {
        "retrieval_config": CharacterRetrievalConfig(),
        "indexing_enabled": indexing_enabled(request.app.state.settings),
    }


@router.put("/api/characters/{character_id}", response_model=CharacterView)
def update_character(character_id: UUID, data: CharacterInput, user: User, db: DB):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    character = _require_character(db, user.id, character_id)
    _require_unique_name(db, character.project_id, data.name, character.id)
    values = data.model_dump()
    saved = CharacterView.model_validate(character).model_dump(include=values.keys())
    if saved != values:
        for key, value in values.items():
            setattr(character, key, value)
        character.revision += 1
    db.commit()
    return character


@router.delete("/api/characters/{character_id}", status_code=204)
def delete_character(character_id: UUID, user: User, db: DB):
    user = lock_account(db, user.id)
    require_initialized(db, user)
    character = _require_character(db, user.id, character_id)
    db.execute(
        delete(IndexState).where(
            IndexState.source_kind == "character", IndexState.source_id == character.id
        )
    )
    db.delete(character)
    db.commit()


@router.get("/api/tasks/{task_id}/characters")
def task_characters(task_id: UUID, user: User, db: DB):
    task = require_writing_task(db, user, task_id)
    rows = db.execute(
        select(CharacterParticipation, Character.name)
        .outerjoin(
            Character,
            (Character.id == CharacterParticipation.character_id)
            & (Character.project_id == task.project_id),
        )
        .where(CharacterParticipation.task_id == task.id)
        .order_by(CharacterParticipation.position)
    )
    return {
        "participations": [
            {
                "position": item.position,
                "character_id": item.character_id if name is not None else None,
                "name": name,
                "revision": item.revision,
                "state": item.state,
                "created_at": item.created_at,
            }
            for item, name in rows
        ]
    }


def adopt_character_names(db: Session, task: Task, names: list[str], body: str) -> None:
    """Called within formal adoption's account-locked transaction; never commits."""
    project = db.scalar(
        select(Project).where(Project.id == task.project_id, Project.user_id == task.user_id)
    )
    if project is None or project.kind != "novel":
        return
    existing = set(db.scalars(select(Character.name).where(Character.project_id == project.id)))
    created = 0
    for name in dict.fromkeys(name.strip() for name in names):
        if not name or len(name) > 60 or name not in body or name in existing:
            continue
        db.add(
            Character(
                project_id=project.id,
                name=name,
                prompt=DEFAULT_CHARACTER_PROMPT,
                known_information="",
                enabled=False,
            )
        )
        created += 1
        if created == 2:
            break


def adopt_character_knowledge(
    db: Session, task: Task, position: int | None, observations: list[KnowledgeObservation]
) -> None:
    """Adopt scene-grounded observations alongside formal writing, without replacing knowledge."""
    if position is None or not observations:
        return
    character = db.scalar(
        select(Character)
        .join(CharacterParticipation, CharacterParticipation.character_id == Character.id)
        .join(Project, Project.id == Character.project_id)
        .where(
            CharacterParticipation.task_id == task.id,
            CharacterParticipation.position == position,
            CharacterParticipation.state == "completed",
            CharacterParticipation.revision == Character.revision,
            Character.project_id == task.project_id,
            Project.user_id == task.user_id,
            Project.kind == "novel",
        )
    )
    if character is None:
        return
    original = character.known_information
    knowledge = original
    for observation in observations:
        if (
            observation.evidence not in character.current_scene
            or observation.evidence in original
            or observation.content in knowledge
        ):
            continue
        label = "事实" if observation.kind == "fact" else "推测"
        updated = (
            knowledge + f"\n\n{label}：{observation.content}\n场景依据：{observation.evidence}"
        )
        if len(updated) <= 6000:
            knowledge = updated
    if knowledge != original:
        character.known_information = knowledge
        character.revision += 1
