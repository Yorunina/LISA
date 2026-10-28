"""Pure corpus coverage, partitioning and call-budget planning before any model call."""

import hashlib
import json
import re
from dataclasses import dataclass

from fastapi import HTTPException

from lisa.chat_corpus import target_windows
from lisa.chat_style import chat_metrics, chat_probes, dependency_groups, synthetic_probes
from lisa.settings import Settings

WRITING_PROBES = (
    {
        "task": "scene",
        "instruction": "写一个全新对白场景：两个人争论是否归还一把借来的伞，"
        "用对话和动作表现分歧，不续写来源作品。",
    },
    {
        "task": "novel",
        "instruction": "写一段全新叙述：修钟人在清晨打开停业已久的店门，"
        "通过观察与动作组织段落，不使用来源人物或剧情。",
    },
)


@dataclass
class AnalysisPlan:
    batches: list[list[dict]]
    development: list[dict] | None
    holdout: list[dict] | None
    coverage: dict
    limitations: list[str]
    calls: int


def _chat_plan(windows, coverage, files, scope, all_segments, targets, settings):
    groups = dependency_groups(windows)
    development = holdout = None
    eligible = [
        group
        for group in groups
        if _size(group) <= settings.style_distillation_chunk_bytes and chat_probes(group, targets)
    ]
    if len(groups) >= 3 and len(eligible) >= 2:
        def strata(group):
            values = set()
            for row in group:
                stamp = str(row.get("timestamp") or "")
                day = stamp[:10] if len(stamp) >= 10 else "unknown-day"
                hour = stamp[11:13] if len(stamp) >= 13 else "unknown-hour"
                try:
                    period = str(int(hour) // 6)
                except ValueError:
                    period = "unknown-period"
                values.add(
                    (
                        str(row.get("conversation") or "unknown-conversation"),
                        day,
                        period,
                        str(row.get("scene") or row.get("window_id") or "unknown-scene"),
                    )
                )
            return values

        # Pick groups that add the most independent conversation/time/scene
        # strata.  This deliberately avoids treating the final two groups as
        # an independent holdout merely because they happen to be last.
        ranked = []
        covered = set()
        for index, group in enumerate(eligible):
            keys = strata(group)
            ranked.append((len(keys - covered), len(keys), -index, group, keys))
            covered |= keys
        ranked.sort(reverse=True, key=lambda item: item[:3])
        development, holdout = ranked[0][3], ranked[1][3]
    train = [group for group in groups if group is not development and group is not holdout]
    batches, batch = [], []
    for group in train:
        by_window = {}
        for row in group:
            by_window.setdefault(row["window_id"], []).append(row)
        for window in by_window.values():
            if batch and _size([*batch, *window]) > settings.style_distillation_chunk_bytes:
                batches.append(batch)
                batch = []
            batch.extend(window)
    if batch:
        batches.append(batch)
    partitions = {
        "distillation": [row for batch in batches for row in batch],
        "development": development or [],
        "holdout": holdout or [],
    }
    # Grouping and the budget are checked before the first paid call.
    seen = set()
    for rows in partitions.values():
        ids = {row["id"] for row in rows}
        if ids & seen:
            raise ValueError("chat_partition_overlap")
        seen |= ids
    count, merge_calls = len(batches), 0
    while count > 4:
        count = (count + 3) // 4
        merge_calls += count
    dev_probes = chat_probes(development, targets) if development else synthetic_probes()
    calls = len(batches) + merge_calls + 1 + 3 * len(dev_probes) + 2
    final_probes = chat_probes(holdout, targets) if holdout else synthetic_probes(regression=True)
    calls += 2 * len(final_probes) + 1
    if calls > settings.style_distillation_max_calls:
        raise HTTPException(
            422,
            f"自然度对照与分析需要 {calls} 次调用，超过本任务上限；"
            "请缩小范围或调整调用配置，未开始分析。",
        )
    unique = {row["id"]: row for group in groups for row in group}
    selected_files = []
    for index, file in enumerate(files):
        rows = [row for row in unique.values() if row["file_index"] == index]
        if rows:
            roles = [
                role
                for role, partition in partitions.items()
                if any(row["file_index"] == index for row in partition)
            ]
            selected_files.append(
                {
                    "name": file["name"],
                    "file_index": index,
                    "selected_segments": len(
                        {row["id"] for row in file["segments"]} & {row["id"] for row in scope}
                    ),
                    "analyzed_segments": len(rows),
                    "chars": sum(len(row["text"]) for row in rows),
                    "source_ids": [row["id"] for row in rows],
                    "roles": roles,
                }
            )
    limitations = [
        "仅分析目标邻近窗口，跨会话和时间有限取样；条数和时间不保证语义完整。",
        "依赖组自动合并重叠、显式引用、较长重复内容及邻近窗口；跨时段指代尚未人工核实。",
        "场景覆盖来自模型对已引用证据的分类，不代表已覆盖全体聊天或全部人格。",
    ]
    if holdout is None:
        limitations.append("未建立独立留出组；合成开发探针只检查表达问题，不证明还原源人物。")
    if coverage["analyzed_target_messages"] < coverage["total_target_messages"]:
        limitations.append("目标消息未全量分析；未覆盖部分不支持风格结论。")
    if coverage["oversized_targets"]:
        limitations.append("超出单窗口预算的目标消息已跳过并计数，未截断。")
    if len(scope) < len(all_segments):
        limitations.append("只分析用户所选范围。")
    parsed_chars = sum(len(row["text"]) for row in all_segments)
    total_chars = sum(len(file["text"]) for file in files)
    if total_chars > parsed_chars:
        limitations.append(f"有 {total_chars - parsed_chars} 字符未能归属已识别片段，未分析。")
    def sampling_strata(rows):
        conversations = {str(row.get("conversation") or "unknown") for row in rows}
        days = {str(row.get("timestamp") or "")[:10] or "unknown" for row in rows}
        scenes = {
            str(row.get("scene") or row.get("window_id") or "unknown") for row in rows
        }
        return {
            "conversations": sorted(conversations),
            "days": sorted(days),
            "scenes": sorted(scenes),
        }
    return AnalysisPlan(
        batches,
        development,
        holdout,
        {
            "total_segments": len(all_segments),
            "selected_segments": len(scope),
            "analyzed_segments": len(unique),
            "total_chars": total_chars,
            "parsed_chars": parsed_chars,
            "selected_chars": sum(len(row["text"]) for row in scope),
            "analyzed_chars": sum(len(row["text"]) for row in unique.values()),
            "sampling": coverage,
            "sampling_strata": sampling_strata([row for group in groups for row in group]),
            "dependency_groups": len(groups),
            "grouping": "dependency_groups_not_human_verified",
            "holdout_strategy": "stratified_conversation_time_scene",
            "files": selected_files,
            "partitions": {
                role: {"segments": len(rows), "chars": sum(len(row["text"]) for row in rows)}
                for role, rows in partitions.items()
            },
            "metrics": chat_metrics(partitions["distillation"], targets),
        },
        limitations,
        calls,
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _split_segment(segment: dict, limit: int) -> list[dict]:
    """Split only oversized units, with explicit locations rather than silent truncation."""
    if _size(segment) <= limit:
        return [segment]
    text = segment["text"]
    pieces, start = [], 0
    while start < len(text):
        low, high = start + 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _size({**segment, "text": text[start:middle]}) <= limit - 100:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(text):
            # Prefer intact paragraphs/messages where the encoding budget allows it.
            boundary = text.rfind("\n", start, end)
            if boundary > start + (end - start) // 2:
                end = boundary + 1
        pieces.append(
            {
                **segment,
                "id": f"{segment['id']}@{start}:{end}",
                "parent_id": segment["id"],
                "text": text[start:end],
            }
        )
        start = end
    return pieces


def _writing_metadata(file: dict, chosen: list[dict]) -> dict:
    """Keep structural writing metadata without retaining headings or prose."""
    text = "".join(segment["text"] for segment in chosen)
    dialogue_marks = sum(text.count(mark) for mark in ('"', "“", "”", "「", "」"))
    return {
        "file": file["name"],
        "chapters": len(chosen),
        "scene_count": len(chosen),
        "genre": "unknown",
        "viewpoint": "unknown",
        "dialogue_tendency": (
            "frequent" if dialogue_marks >= max(4, len(chosen) * 2) else "limited"
        ),
        "metadata_basis": "structural_counts_only",
    }


def build_plan(
    payload: dict,
    kind: str,
    targets: list[str],
    selection: list[str],
    settings: Settings,
) -> AnalysisPlan:
    """Cover every selected unit; fail before any paid call if the plan cannot fit."""
    files = payload.get("files", [])
    all_segments = [segment for file in files for segment in file["segments"]]
    known = {segment["id"] for segment in all_segments}
    selected_ids = set(selection) if selection else known
    if not selected_ids or selected_ids - known:
        raise HTTPException(422, "分析范围为空或包含无法识别的片段，请重新选择。")
    speakers = {segment.get("speaker") for segment in all_segments} - {None}
    if kind == "personality" and (not targets or set(targets) - speakers):
        raise HTTPException(422, "请选择已识别的目标发言者；多文件身份需要明确勾选。")
    if kind == "personality" and not any(
        segment["id"] in selected_ids and segment.get("speaker") in targets
        for segment in all_segments
    ):
        raise HTTPException(422, "所选范围没有目标发言者的消息。")
    if settings.style_distillation_chunk_bytes > settings.style_distillation_max_input_bytes // 2:
        raise HTTPException(422, "服务端分块配置超过单次输入预算，请调整分析配置。")

    scope = [segment for segment in all_segments if segment["id"] in selected_ids]
    sampled, window_coverage = {}, None
    if kind == "personality":
        indexed = [
            {**segment, "file": file["name"], "file_index": index}
            for index, file in enumerate(files)
            for segment in file["segments"]
            if segment["id"] in selected_ids
        ]
        windows, window_coverage = target_windows(
            indexed,
            targets,
            before=settings.style_chat_context_before,
            after=settings.style_chat_context_after,
            max_windows=min(
                settings.style_chat_max_windows,
                max(1, (settings.style_distillation_max_calls - 2) // 2),
            ),
            max_bytes=min(16_000, settings.style_distillation_chunk_bytes - 512),
        )
        for window in windows:
            sampled.setdefault(window[0]["file_index"], []).append(window)
        if not windows:
            raise HTTPException(422, "目标消息无法放入分析窗口，请选择较短的消息；未开始分析。")
        plan = _chat_plan(windows, window_coverage, files, scope, all_segments, targets, settings)
        if payload.get("group_sampling"):
            plan.coverage["group_source"] = payload["group_sampling"]
        return plan

    batches, groups, coverage_files, limitations = [], [], [], []
    analyzed_chars, analyzed_segments = 0, 0
    partial_context = False
    fingerprints: set[str] = set()
    duplicate_sources = False
    for file_index, file in enumerate(files):
        source_groups = sampled.get(file_index, []) if kind == "personality" else None
        chosen = (
            list({row["id"]: row for window in source_groups for row in window}.values())
            if kind == "personality"
            else [s for s in file["segments"] if s["id"] in selected_ids]
        )
        if not chosen:
            continue
        units = []
        for segment in chosen:
            source = {**segment, "file": file["name"], "file_index": file_index}
            if kind == "writing":
                parts = _split_segment(source, settings.style_distillation_chunk_bytes - 256)
                partial_context |= len(parts) > 1
                units.extend(parts)
            text = segment["text"]
            if kind == "personality" and "\n" in text:
                text = text.split("\n", 1)[1]  # A changed QQ export header is not a new sample.
            normalized = _normalized(text)
            if len(normalized) >= 32:
                windows = (
                    [normalized]
                    if len(normalized) < 64
                    else [
                        normalized[start : start + 64]
                        for start in range(0, len(normalized) - 63, 32)
                    ]
                )
                hashes = {hashlib.sha256(window.encode()).hexdigest() for window in windows}
                duplicate_sources |= bool(hashes & fingerprints)
                fingerprints.update(hashes)
        file_batches, batch = [], []
        for unit_group in source_groups if kind == "personality" else [[unit] for unit in units]:
            if batch and _size([*batch, *unit_group]) > settings.style_distillation_chunk_bytes:
                file_batches.append(batch)
                batch = []
            batch.extend(unit_group)
        if batch:
            file_batches.append(batch)
        batches.extend(file_batches)
        groups.append(file_batches)
        chars = sum(len(s["text"]) for s in chosen)
        analyzed_chars += chars
        analyzed_segments += len(chosen)
        coverage_files.append(
            {
                "name": file["name"],
                "file_index": file_index,
                "selected_segments": sum(s["id"] in selected_ids for s in file["segments"]),
                "analyzed_segments": len(chosen),
                "chars": chars,
                "source_ids": [s["id"] for s in chosen],
                **(
                    {"work_metadata": _writing_metadata(file, chosen)}
                    if kind == "writing"
                    else {}
                ),
            }
        )
    development = holdout = None
    # Different whole files are the most conservative automatically available groups.
    # We do not pretend adjacent chunks of a single novel/conversation are independent.
    if len(groups) >= 3 and not duplicate_sources and len(groups[-1]) == len(groups[-2]) == 1:
        holdout, development = groups[-1][0], groups[-2][0]
        batches = batches[:-2]
        limitations.append(
            "开发与留出按完整不同文件隔离，自动检查重复片段；跨文件情节或指代依赖尚未人工核实。"
        )
    else:
        limitations.append(
            "未建立独立留出组：需至少三份可分离的非重复文件，开发与留出各须在单次预算内；"
            "同一长篇相邻章节或同一聊天的切块不冒充独立留出。"
        )
    if kind == "personality":
        limitations.append(
            "仅分析目标发言及同会话邻近窗口，按会话和时间分层有限取样；"
            "其余群成员的无关消息不进入模型。条数/时间边界不保证话题完整，缺失依赖不作推测。"
        )
        if window_coverage["analyzed_target_messages"] < window_coverage["total_target_messages"]:
            limitations.append(
                "目标消息已按窗口预算取样，不是全量分析；未覆盖范围不能用于风格结论。"
            )
        if window_coverage["oversized_targets"]:
            limitations.append("存在单条超出窗口预算的目标消息，已跳过且计入覆盖统计，未截断原文。")
    if partial_context:
        limitations.append("超出单批预算的长片段已完整分段覆盖，分段处的完整语境可能不足。")
    if len(scope) < len(all_segments):
        limitations.append("仅分析用户选择的范围，未选择部分不计入风格结论。")
    total_chars = sum(len(file["text"]) for file in files)
    parsed_chars = sum(len(s["text"]) for s in all_segments)
    if total_chars > parsed_chars:
        limitations.append(
            f"有 {total_chars - parsed_chars} 字符未能归属已识别片段，未分析这些内容。"
        )
    if duplicate_sources:
        limitations.append("发现重复片段，未将重复材料分到独立验证组。")
    # Reduce observations hierarchically rather than putting an entire novel's
    # reports into one synthesis request. Count these calls before charging anything.
    count, merge_calls = len(batches), 0
    while count > 4:
        count = (count + 3) // 4
        merge_calls += count
    # Two short writing tasks plus one review; without holdout these are
    # explicitly synthetic checks, never evidence of source-style fidelity.
    verification_calls = len(WRITING_PROBES) + 1
    calls = len(batches) + merge_calls + 1 + (1 if development else 0) + verification_calls
    if calls > settings.style_distillation_max_calls:
        raise HTTPException(
            422,
            f"所选范围需要 {calls} 次调用，超过本任务上限 "
            f"{settings.style_distillation_max_calls} 次。请缩小选区或分批上传；未开始分析。",
        )

    def segment_count(units):
        return len({unit.get("parent_id", unit["id"]) for unit in units})

    def partition_chars(units):
        values = {unit["id"]: unit for unit in units}.values() if kind == "personality" else units
        return sum(len(unit["text"]) for unit in values)

    partitions = {
        "distillation": [unit for batch in batches for unit in batch],
        "development": development or [],
        "holdout": holdout or [],
    }
    for entry in coverage_files:
        entry["role"] = next(
            role
            for role, units in partitions.items()
            if any(unit["file_index"] == entry["file_index"] for unit in units)
        )
    group_source = payload.get("group_sampling") if kind == "personality" else None
    if group_source:
        limitations.append(
            "群档案先由数据库筛选与取样，再分析目标邻近窗口；"
            "数据库命中总数、临时快照和实际入模覆盖分开统计，未回溯NTQQ全部历史。"
        )
    return AnalysisPlan(
        batches=batches,
        development=development,
        holdout=holdout,
        coverage={
            "total_segments": len(all_segments),
            "selected_segments": len(scope),
            "analyzed_segments": analyzed_segments,
            "total_chars": total_chars,
            "parsed_chars": parsed_chars,
            "selected_chars": sum(len(s["text"]) for s in scope),
            "analyzed_chars": analyzed_chars,
            **({"sampling": window_coverage} if window_coverage else {}),
            **({"group_source": group_source} if group_source else {}),
            "partitions": {
                role: {
                    "segments": segment_count(units),
                    "chars": partition_chars(units),
                }
                for role, units in partitions.items()
            },
            "files": coverage_files,
        },
        limitations=limitations,
        calls=calls,
    )
