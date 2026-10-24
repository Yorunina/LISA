"""Saved instructions and explicit, version-pinned applications."""

from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lisa.auth import DB
from lisa.models import (
    Persona,
    PersonaVersion,
    Project,
    ProjectStyleOverride,
    QQBinding,
    StyleApplication,
    StyleProfile,
    StyleProfileVersion,
    Task,
)
from lisa.style_corpus import StyleKind, StyleUser, clear_expired_corpora
from lisa.works import lock_account, require_project

router = APIRouter(tags=["style"])


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=12000)
    summary: str = Field(default="", max_length=2400)
    report: dict = Field(default_factory=dict)
    source_task_id: UUID | None = None


class CreateProfile(VersionInput):
    kind: StyleKind
    name: str = Field(min_length=1, max_length=80)


class VersionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID


class OverrideReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID | None = None


def require_profile(db: Session, user_id: UUID, profile_id: UUID) -> StyleProfile:
    profile = db.scalar(
        select(StyleProfile).where(StyleProfile.id == profile_id, StyleProfile.user_id == user_id)
    )
    if profile is None:
        raise HTTPException(404, "风格方案不存在。")
    return profile


def require_version(
    db: Session, user_id: UUID, version_id: UUID, kind: str | None = None
) -> tuple[StyleProfile, StyleProfileVersion]:
    row = db.execute(
        select(StyleProfile, StyleProfileVersion)
        .join(StyleProfileVersion, StyleProfileVersion.profile_id == StyleProfile.id)
        .where(StyleProfile.user_id == user_id, StyleProfileVersion.id == version_id)
    ).first()
    if row is None or (kind and row[0].kind != kind):
        raise HTTPException(404, "该类型的风格版本不存在。")
    return row[0], row[1]


def version_view(version: StyleProfileVersion) -> dict:
    return {
        key: getattr(version, key)
        for key in ("id", "version", "prompt", "summary", "report", "source_task_id", "created_at")
    }


def profile_view(db: Session, profile: StyleProfile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "kind": profile.kind,
        "latest_version": profile.latest_version,
        "versions": [
            version_view(v)
            for v in db.scalars(
                select(StyleProfileVersion)
                .where(StyleProfileVersion.profile_id == profile.id)
                .order_by(StyleProfileVersion.version.desc())
            )
        ],
    }


def applications_view(db: Session, user_id: UUID) -> dict:
    result = {"personality": None, "writing": None}
    result.update(
        {
            item.kind: item.version_id
            for item in db.scalars(
                select(StyleApplication).where(StyleApplication.user_id == user_id)
            )
        }
    )
    return result


def clear_personality_application(db: Session, user_id: UUID) -> None:
    db.execute(
        delete(StyleApplication).where(
            StyleApplication.user_id == user_id, StyleApplication.kind == "personality"
        )
    )


def resolve_writing_style(db: Session, user_id: UUID, project_id: UUID) -> dict:
    # Ownership is checked even for internal consumers to prevent cross-account default fallback.
    project = db.scalar(
        select(Project.id).where(Project.id == project_id, Project.user_id == user_id)
    )
    if project is None:
        raise HTTPException(404, "作品不存在。")
    override = db.get(ProjectStyleOverride, project_id)
    default = db.get(StyleApplication, (user_id, "writing"))
    version_id = override.version_id if override else default.version_id if default else None
    profile, version = (
        require_version(db, user_id, version_id, "writing") if version_id else (None, None)
    )
    return {
        "override_version_id": str(override.version_id) if override else None,
        "effective_version_id": str(version_id) if version_id else None,
        "effective_profile_name": profile.name if profile else None,
        "prompt": version.prompt if version else "",
        "source": "override" if override else "default" if default else "none",
    }


def _new_version(
    db: Session, user_id: UUID, profile: StyleProfile, data: VersionInput
) -> StyleProfileVersion:
    if data.source_task_id:
        task = db.scalar(
            select(Task).where(
                Task.id == data.source_task_id,
                Task.user_id == user_id,
                Task.kind.in_({"style_distill", "style_trial"}),
            )
        )
        if task is None or task.status != "completed" or not task.result:
            raise HTTPException(422, "来源蒸馏任务不可用。")
        if task.kind == "style_trial" and task.request.get("mode") != "refine":
            raise HTTPException(422, "普通试用不能作为性格方案来源。")
        if task.request.get("kind") != profile.kind:
            raise HTTPException(422, "来源任务与方案类型不一致。")
    if not data.prompt.strip():
        raise HTTPException(422, "风格指引不能为空。")
    # Reports are generated by the pipeline. Client edits cannot smuggle source copies
    # into a long-lived evidence object; only a verified task report is persisted.
    report, evaluated_prompt = {}, None
    if data.source_task_id:
        report = deepcopy(task.result.get("report", {}))
        evaluated_prompt = task.result.get("prompt")
    elif profile.latest_version > 1:
        previous = db.scalar(
            select(StyleProfileVersion).where(
                StyleProfileVersion.profile_id == profile.id,
                StyleProfileVersion.version == profile.latest_version - 1,
            )
        )
        if previous:
            report = deepcopy(previous.report)
            evaluated_prompt = previous.prompt
    if report and evaluated_prompt is not None and data.prompt.strip() != evaluated_prompt:
        original = report.get("validation", {})
        report["edited_from_source"] = True
        report["validation"] = {
            "status": "edited_not_evaluated",
            "note": "指引已编辑；已有验证仅针对编辑前候选，当前版本尚未验证。",
            "original_candidate_validation": original.get(
                "original_candidate_validation", original
            ),
        }
    version = StyleProfileVersion(
        profile_id=profile.id,
        version=profile.latest_version,
        prompt=data.prompt.strip(),
        summary=data.summary.strip(),
        report=report,
        source_task_id=data.source_task_id,
    )
    db.add(version)
    db.flush()
    return version


@router.get("/api/style/profiles")
def list_profiles(user: StyleUser, db: DB):
    clear_expired_corpora(db)
    db.commit()
    return {
        "profiles": [
            profile_view(db, p)
            for p in db.scalars(
                select(StyleProfile)
                .where(StyleProfile.user_id == user.id)
                .order_by(StyleProfile.created_at.desc())
            )
        ],
        "applications": applications_view(db, user.id),
    }


@router.post("/api/style/profiles", status_code=201)
def create_profile(data: CreateProfile, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    profile = StyleProfile(
        user_id=user.id, kind=data.kind, name=data.name.strip(), latest_version=1
    )
    if not profile.name:
        raise HTTPException(422, "方案名称不能为空。")
    db.add(profile)
    db.flush()
    _new_version(db, user.id, profile, data)
    db.commit()
    return profile_view(db, profile)


@router.post("/api/style/profiles/{profile_id}/versions", status_code=201)
def save_version(profile_id: UUID, data: VersionInput, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    profile = require_profile(db, user.id, profile_id)
    profile.latest_version += 1
    _new_version(db, user.id, profile, data)
    db.commit()
    return profile_view(db, profile)


@router.post("/api/style/profiles/{profile_id}/restore", status_code=201)
def restore_version(profile_id: UUID, data: VersionReference, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    profile = require_profile(db, user.id, profile_id)
    old_profile, old = require_version(db, user.id, data.version_id)
    if old_profile.id != profile.id:
        raise HTTPException(422, "版本不属于该方案。")
    profile.latest_version += 1
    db.add(
        StyleProfileVersion(
            profile_id=profile.id,
            version=profile.latest_version,
            prompt=old.prompt,
            summary=old.summary,
            report=deepcopy(old.report),
            source_task_id=old.source_task_id,
        )
    )
    db.commit()
    return profile_view(db, profile)


@router.put("/api/style/applications/{kind}")
def apply_version(kind: StyleKind, data: VersionReference, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    if user is None or not user.active:
        raise HTTPException(401, "账号不可用。")
    if kind == "personality" and db.get(QQBinding, user.id) is None:
        raise HTTPException(403, "请先完成 QQ 绑定。")
    profile, version = require_version(db, user.id, data.version_id, kind)
    item = db.get(StyleApplication, (user.id, kind))
    if item:
        item.version_id = version.id
    else:
        db.add(StyleApplication(user_id=user.id, kind=kind, version_id=version.id))
    if kind == "personality":
        persona = db.scalar(select(Persona).where(Persona.user_id == user.id))
        if persona is None:
            persona = Persona(user_id=user.id, name=profile.name, prompt=version.prompt, version=1)
            db.add(persona)
            db.flush()
        else:
            if persona.name != profile.name or persona.prompt != version.prompt:
                persona.name, persona.prompt = profile.name, version.prompt
                persona.version += 1
        if db.get(PersonaVersion, (persona.id, persona.version)) is None:
            db.add(
                PersonaVersion(
                    persona_id=persona.id,
                    version=persona.version,
                    name=persona.name,
                    prompt=persona.prompt,
                )
            )
    db.commit()
    return applications_view(db, user.id)


@router.get("/api/projects/{project_id}/writing-style")
def get_project_style(project_id: UUID, user: StyleUser, db: DB):
    return resolve_writing_style(db, user.id, project_id)


@router.put("/api/projects/{project_id}/writing-style")
def update_project_style(project_id: UUID, data: OverrideReference, user: StyleUser, db: DB):
    user = lock_account(db, user.id)
    require_project(db, user, project_id)
    override = db.get(ProjectStyleOverride, project_id)
    if data.version_id is None:
        if override:
            db.delete(override)
    else:
        require_version(db, user.id, data.version_id, "writing")
        if override:
            override.version_id = data.version_id
        else:
            db.add(ProjectStyleOverride(project_id=project_id, version_id=data.version_id))
    db.commit()
    return resolve_writing_style(db, user.id, project_id)
