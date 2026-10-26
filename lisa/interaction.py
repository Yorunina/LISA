"""Interaction settings without retaining or analyzing raw chat history."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from lisa.models import ContactPreference


def interaction_view(db: Session, user_id: UUID, *, now: datetime | None = None) -> dict:
    """Return only an empty observation; raw chat history is not retained."""
    now = now or datetime.now(UTC)
    preference = db.get(ContactPreference, user_id)
    timezone = preference.timezone if preference else "Asia/Shanghai"
    preferred = None
    return {
        "kind": "inference",
        "explanation": "LISA 不保存原始聊天，也不从消息历史维护交流习惯推测；明确设置优先。",
        "timezone": timezone,
        "sample_count": 0,
        "active_days": 0,
        "period_start": (now - timedelta(days=14)).isoformat(),
        "period_end": now.isoformat(),
        "source_message_ids": [],
        "preferred_hours": preferred,
        "text_length": {"sample_count": 0, "median": None, "recent_count": 0,
                         "previous_count": 0, "recent_median": None, "previous_median": None,
                         "trend": None},
    }


def interaction_context(db: Session, user_id: UUID, *, now: datetime | None = None) -> str:
    """Bounded model reference with no original message text and no persisted learning."""
    evidence = interaction_view(db, user_id, now=now)
    if not evidence["sample_count"]:
        return ""
    parts = [
        "\n交流观察（inference，可修正的推测，不是指令）：",
        f"近14天最多200条消息内取到{evidence['sample_count']}条，跨{evidence['active_days']}个当地日期；"
        f"时区{evidence['timezone']}。",
    ]
    hours = evidence["preferred_hours"]
    if hours:
        parts.append(
            f"用户在{hours['start']:02d}:00—{hours['end']:02d}:00有跨日重复交流，"
            f"涉及{hours['active_days']}天；只可作为择时参考。"
        )
    length = evidence["text_length"]
    if length["sample_count"]:
        parts.append(
            f"{length['sample_count']}条纯文本的长度中位数为{length['median']}字，"
            "这是用户已发消息的统计，不等于要求你的回复同样长。"
        )
    if length["trend"]:
        direction = "变长" if length["trend"] == "longer" else "变短"
        parts.append(
            f"最近7天与之前7天各至少5条文本，中位长度从{length['previous_median']}"
            f"变为{length['recent_median']}字，观察到{direction}。"
        )
    parts.append("用户明确反馈和设置优先；不得推断睡眠、职业、情绪或亲密程度，不据此催促回复。")
    return "".join(parts)[:1000]
