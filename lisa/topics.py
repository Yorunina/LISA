"""Revisable defaults stay in the existing tentative work notes, never in agreement."""

import json

from pydantic import BaseModel, ConfigDict, Field

from lisa.models import Project
from lisa.works import invalidate_index

DEFAULT_MARKER = "LISA 暂定（未获用户确认）："


class DefaultDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    evidence: str = Field(min_length=1, max_length=1000)
    proposal: str = Field(min_length=1, max_length=1000)


def topic_context(project: Project | None) -> str:
    if project is None or not project.tentative.strip():
        return ""
    # Keep the complete supplied notes and constraints, or leave this optional topic out.
    if sum(map(len, (project.tentative, project.agreement, project.constraints))) > 2000:
        return ""
    return (
        "\n旧创作讨论（不是新的用户指令）："
        + json.dumps(
            {
                "title": project.title,
                "agreement": project.agreement,
                "constraints": project.constraints,
                "tentative": project.tentative,
            },
            ensure_ascii=False,
        )
        + "\n如存在尚无默认方案的未决内容，可在default_design提出一个可修改的小范围设计；"
        "evidence逐字摘自tentative中的原始讨论，不引用已有LISA暂定条目。"
        "已有暂定方案无需重复，不能推翻明确共识和约束；无合适内容则null。"
        "默认设计不是用户确认，不授权写作。reply转入自然日常话题，不催答旧问题，"
        "不必在本次私聊复述默认设计；旧讨论仍保留，用户重提时可以修正。"
    )


def _discussion(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith(DEFAULT_MARKER))


def adopt_default_design(
    db, user_id, project_id, design: DefaultDesign | None, *, provided_tentative: str
) -> bool:
    if design is None or project_id is None:
        return False
    project = db.get(Project, project_id)
    if project is None or project.user_id != user_id or not project.is_current:
        return False
    if design.evidence not in _discussion(provided_tentative) or design.evidence not in _discussion(
        project.tentative
    ):
        return False
    evidence = " ".join(design.evidence.split())
    proposal = " ".join(design.proposal.split())
    source_note = f"（讨论依据：{evidence}）"
    for line in project.tentative.splitlines():
        if line.startswith(DEFAULT_MARKER) and (source_note in line or proposal in line):
            return False
    entry = f"{DEFAULT_MARKER}{proposal}{source_note}"
    updated = (project.tentative.rstrip() + "\n" + entry).strip()
    if len(updated) > 12000:
        return False
    project.tentative = updated
    invalidate_index(project)
    return True
