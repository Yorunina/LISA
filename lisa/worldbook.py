"""Small, project-owned setting entries; no world simulation or inferred facts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorldbookEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=100)
    category: Literal["rule", "place", "faction", "history", "other"] = "other"
    content: str = Field(min_length=1, max_length=4000)
    scope: str = Field(default="", max_length=200)
    related_ids: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))

    @field_validator("title", "content", "scope", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


def validate_entries(entries: list[WorldbookEntry]) -> list[WorldbookEntry]:
    ids = {item.id for item in entries}
    if len(ids) != len(entries):
        raise ValueError("设定条目标识不能重复。")
    if sum(len(item.content) for item in entries) > 200000:
        raise ValueError("设定条目正文合计最多 200000 字符。")
    for item in entries:
        if (
            len(set(item.related_ids)) != len(item.related_ids)
            or item.id in item.related_ids
            or not set(item.related_ids) <= ids
        ):
            raise ValueError("关联设定必须是本作品中存在的其他条目，且不能重复。")
    return entries


CATEGORY_NAMES = {
    "rule": "规则",
    "place": "地点",
    "faction": "组织",
    "history": "历史",
    "other": "其他",
}


def entry_text(item: dict) -> str:
    heading = f"设定：{item['title']}（{CATEGORY_NAMES[item['category']]}）"
    if item.get("scope"):
        heading += f"\n适用条件：{item['scope']}"
    return heading + "\n" + item["content"]
