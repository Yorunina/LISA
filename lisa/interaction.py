"""Read-only interaction observations from at most 200 valid QQ messages in 14 days."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.models import ChatMessage, ContactPreference, Task


def interaction_view(db: Session, user_id: UUID, *, now: datetime | None = None) -> dict:
    """Statistics are observations; time/length suggestions remain revisable inferences."""
    now = now or datetime.now(UTC)
    start = now - timedelta(days=14)
    preference = db.get(ContactPreference, user_id)
    timezone = preference.timezone if preference else "Asia/Shanghai"
    zone = ZoneInfo(timezone)
    messages = db.scalars(
        select(ChatMessage)
        .join(Task, Task.id == ChatMessage.task_id)
        .where(
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
            ChatMessage.deleted_at.is_(None),
            ChatMessage.context_excluded.is_(False),
            ChatMessage.sent_at >= start,
            ChatMessage.sent_at <= now,
            Task.user_id == user_id,
            Task.kind == "chat",
            Task.source == "qq",
            Task.status == "completed",
            Task.error.is_(None),
        )
        .order_by(ChatMessage.sent_at.desc(), ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(200)
    ).all()
    day_slots = {
        (message.sent_at.astimezone(zone).date(), message.sent_at.astimezone(zone).hour // 6)
        for message in messages
    }
    active_days = len({day for day, _ in day_slots})
    votes = Counter(slot for _, slot in day_slots)
    preferred = None
    if votes:
        ranked = votes.most_common()
        slot, days = ranked[0]
        if days >= 3 and days * 2 >= active_days and (len(ranked) == 1 or days > ranked[1][1]):
            preferred = {"start": slot * 6, "end": (slot + 1) * 6, "active_days": days}

    texts = [message for message in messages if message.kind == "text"]
    recent = [message for message in texts if message.sent_at >= now - timedelta(days=7)]
    previous = [message for message in texts if message.sent_at < now - timedelta(days=7)]
    recent_median = median([len(message.content) for message in recent]) if recent else None
    previous_median = median([len(message.content) for message in previous]) if previous else None
    trend = None
    if len(recent) >= 5 and len(previous) >= 5:
        delta = recent_median - previous_median
        if abs(delta) >= max(10, previous_median * 0.25):
            trend = "longer" if delta > 0 else "shorter"
    return {
        "kind": "inference",
        "explanation": (
            "仅据近期有效 QQ 用户消息推测交流习惯，不代表作息、职业、情绪或亲密程度；明确设置优先。"
        ),
        "timezone": timezone,
        "sample_count": len(messages),
        "active_days": active_days,
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        "source_message_ids": [str(message.id) for message in messages],
        "preferred_hours": preferred,
        "text_length": {
            "sample_count": len(texts),
            "median": median([len(message.content) for message in texts]) if texts else None,
            "recent_count": len(recent),
            "previous_count": len(previous),
            "recent_median": recent_median,
            "previous_median": previous_median,
            "trend": trend,
        },
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
