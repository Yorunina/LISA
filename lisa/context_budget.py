"""Deterministic context packing shared by model callers.

The packer only selects complete, caller-provided sections.  It does not
summarize, rewrite, or infer facts from omitted material; callers decide which
sections are authoritative and which can be recovered through retrieval.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ContextItem:
    """One independently selectable context field."""

    key: str
    value: Any
    priority: int = 0
    required: bool = False


@dataclass(frozen=True)
class PackedContext:
    """Selected fields and deterministic packing diagnostics."""

    values: dict[str, Any]
    included: tuple[str, ...]
    omitted: tuple[str, ...]
    measured_bytes: int


class ContextBudgetExceeded(ValueError):
    """Required context cannot fit the caller's complete request budget."""


def json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def pack_mapping(
    items: list[ContextItem],
    limit: int,
    *,
    measure: Callable[[dict[str, Any]], int] | None = None,
) -> PackedContext:
    """Pack complete mapping fields under ``limit`` using caller priorities.

    Required items are always retained or raise ``ContextBudgetExceeded``.
    Optional items are considered by descending priority while preserving their
    input order for ties.  The default measure is compact JSON size; callers
    with a full model request can provide a measure that includes instructions
    and the response schema.
    """

    if limit < 1:
        raise ValueError("context budget must be positive")
    measure = measure or json_bytes
    selected: dict[str, Any] = {}
    included: list[str] = []
    omitted: list[str] = []

    required = [item for item in items if item.required]
    optional = [item for item in items if not item.required]
    for item in required:
        if item.key in selected:
            raise ValueError(f"duplicate context key: {item.key}")
        selected[item.key] = item.value
        included.append(item.key)
    if measure(selected) > limit:
        raise ContextBudgetExceeded("required context exceeds the model input budget")

    for item in sorted(enumerate(optional), key=lambda pair: (-pair[1].priority, pair[0])):
        _, candidate = item
        if candidate.key in selected:
            raise ValueError(f"duplicate context key: {candidate.key}")
        trial = {**selected, candidate.key: candidate.value}
        if measure(trial) <= limit:
            selected = trial
            included.append(candidate.key)
        else:
            omitted.append(candidate.key)

    return PackedContext(
        values=selected,
        included=tuple(included),
        omitted=tuple(omitted),
        measured_bytes=measure(selected),
    )
