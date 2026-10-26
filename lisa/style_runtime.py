"""Deterministic, bounded style-card selection for runtime model calls.

The distillation report is diagnostic data.  Runtime callers use only the small
card produced here, so a large report can never silently become prompt context.
"""

from __future__ import annotations

import re

CHAT_STYLE_CARD_BYTES = 4_000
WRITING_STYLE_CARD_BYTES = 6_000
CHAT_SCENES = (
    "casual",
    "question",
    "support",
    "disagreement",
    "humor",
    "boundary",
    "closing",
)
WRITING_TASKS = ("scene", "rewrite", "diary", "novel")


def utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def fit_utf8(value: str, limit: int) -> str:
    """Keep a string within a byte limit without splitting a UTF-8 codepoint."""
    if limit < 1:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    # Decode with ignore after cutting at the byte boundary.  This is safe for
    # UTF-8 and is deterministic for both Chinese and mixed ASCII text.
    clipped = encoded[:limit].decode("utf-8", errors="ignore").rstrip()
    return clipped


def _text(value: object) -> str:
    return str(value or "").strip()


def _rule(item: dict, *, kind: str) -> dict:
    choice_key = "write_choice" if kind == "writing" else "response_choice"
    choice = _text(item.get(choice_key) or item.get("response_choice"))
    return {
        "condition": _text(item.get("condition")) or "适合当前上下文时",
        "choice": choice or "先回应当前重点，再决定是否需要展开。",
        "counterexample": _text(item.get("counterexample")) or "材料不足以判断时保持未知。",
        "support_groups": item.get("support_groups", item.get("support_group_count")),
        "uncertainty": _text(item.get("uncertainty")) or "仅在观察到的范围内使用。",
        **({"scene": item.get("scene", "other")} if kind == "personality" else {}),
        **({"task": item.get("task", item.get("scene", "novel"))} if kind == "writing" else {}),
    }


def select_chat_rules(profile: dict, scene: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return at most five core and two scene rules from a structured profile."""
    rules = list(
        profile.get("rules")
        or profile.get("core_rules", [])
        or profile.get("evidence", [])
        or []
    )
    scene_rules = profile.get("scene_rules", {})
    if scene in CHAT_SCENES and isinstance(scene_rules, dict) and scene_rules.get(scene):
        rules = [
            *[
                item
                for item in rules
                if isinstance(item, dict) and item.get("scene") not in CHAT_SCENES
            ],
            *[item for item in scene_rules[scene] if isinstance(item, dict)],
        ]
    normalized_scene = scene if scene in CHAT_SCENES else None
    core, selected = [], []
    for item in rules:
        if not isinstance(item, dict):
            continue
        item_scene = item.get("scene")
        target = selected if normalized_scene and item_scene == normalized_scene else core
        if target is core and item_scene in CHAT_SCENES and normalized_scene:
            continue
        if len(target) < (2 if target is selected else 5):
            target.append(item)
    # If no core rules were explicitly marked, use the first available rules as
    # global guidance, while still keeping the scene quota bounded.
    if not core:
        for item in rules:
            if isinstance(item, dict) and item not in selected and len(core) < 5:
                core.append(item)
    return core, selected


def select_writing_rules(profile: dict, task: str | None = None) -> list[dict]:
    """Select rules for an existing writing task boundary."""
    normalized_task = task if task in WRITING_TASKS else "novel"
    rules = list(
        profile.get("rules")
        or profile.get("evidence", [])
        or []
    )
    task_rules = profile.get("task_rules", {})
    if isinstance(task_rules, dict) and task_rules.get(normalized_task):
        rules = [item for item in task_rules[normalized_task] if isinstance(item, dict)]
    selected = [
        item
        for item in rules
        if isinstance(item, dict)
        and item.get("task", item.get("scene")) in {normalized_task, "all", None}
    ]
    if not selected:
        selected = [item for item in rules if isinstance(item, dict)]
    return selected


def _render_rule(item: dict, *, kind: str) -> str:
    rule = _rule(item, kind=kind)
    label = "写作选择" if kind == "writing" else "回应选择"
    scope = rule.get("task") if kind == "writing" else rule.get("scene")
    scope_text = f"[{scope}] " if scope and scope not in {"other", "all"} else ""
    return (
        f"{scope_text}条件：{rule['condition']}；{label}：{rule['choice']}；"
        f"反例：{rule['counterexample']}；不确定：{rule['uncertainty']}"
    )


def compile_runtime_style_card(
    report: dict | None,
    kind: str,
    *,
    scene: str | None = None,
    task: str | None = None,
    fallback: str = "",
) -> str:
    """Compile only bounded rules, never the complete report, for a model call."""
    profile = (report or {}).get("profile", report or {})
    if kind == "personality":
        core, selected = select_chat_rules(profile, scene)
        sections = [
            "交流风格卡：只影响表达方式，服从当前事实、权限和用户明确要求。",
            *(_render_rule(item, kind=kind) for item in core),
            *(_render_rule(item, kind=kind) for item in selected),
        ]
        limit = CHAT_STYLE_CARD_BYTES
    else:
        selected = select_writing_rules(profile, task)
        sections = [
            "写作风格卡：只影响语言、叙事和节奏，不移植样本人物、经历、设定或剧情。",
            *(_render_rule(item, kind=kind) for item in selected),
        ]
        limit = WRITING_STYLE_CARD_BYTES
    card = "\n".join(section for section in sections if section)
    has_rules = bool(
        profile.get("rules")
        or profile.get("core_rules")
        or profile.get("evidence")
        or profile.get("task_rules")
    )
    if not selected and not has_rules and fallback:
        card = fallback
    return fit_utf8(card, limit)


def chat_scene(text: str) -> str:
    """Small deterministic scene hint; classification never calls another model."""
    value = _text(text)
    if re.search(r"先去|晚点|下次|晚安|再见|拜拜", value):
        return "closing"
    if re.search(r"不想|别问|不要|拒绝|不方便|停一下", value):
        return "boundary"
    if re.search(r"哈哈|笑死|好笑|玩笑|😂|🤣", value):
        return "humor"
    if re.search(r"难过|烦|累|委屈|担心|焦虑|安慰", value):
        return "support"
    if re.search(r"不同意|不对|可是|但是|应该|你也这么", value):
        return "disagreement"
    if "?" in value or "？" in value or re.search(r"怎么|为什么|能不能|可以吗", value):
        return "question"
    return "casual"


def writing_task(project_kind: str | None, task_kind: str | None) -> str:
    if project_kind == "diary":
        return "diary"
    if task_kind in {"scene", "rewrite"}:
        return task_kind
    return "novel"
