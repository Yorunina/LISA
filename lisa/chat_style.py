"""Bounded chat-style evidence, dependency groups and evaluation inputs; no I/O."""

import hashlib
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

SCENES = ("casual", "question", "support", "disagreement", "humor", "boundary", "closing", "other")
BASELINE = "你是文学创作伙伴 LISA，用中文回应当前消息。"


def expression_guide() -> str:
    return (Path(__file__).parent / "prompts" / "chat_expression.txt").read_text(encoding="utf-8")


def message_body(row: dict) -> str:
    text = row["text"]
    if re.match(r"^[^\n]+:\n时间: [^\n]+\n内容:", text):
        return re.sub(r"^[^\n]+:\n时间: [^\n]+\n内容:[ \t]*", "", text, count=1)
    if text.startswith("时间:"):
        return text.split("\n", 1)[-1]
    return re.sub(
        r"^(?:\[?\d{4}[-/]\d{1,2}[-/]\d{1,2}.*|[^\n]+\s\d{4}[-/]\d{1,2}[-/]\d{1,2}.*)\n",
        "",
        text,
        count=1,
    )


def dependency_groups(windows: list[list[dict]]) -> list[list[dict]]:
    """Union overlap, explicit replies, substantial copies and nearby conversation windows.

    Time-separated groups are candidates, not proof of semantic independence. Missing
    timestamps keep a conversation together. Short acknowledgments are not duplicates.
    """
    parents = list(range(len(windows)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        parents[root(right)] = root(left)

    owners, copies, conversations = {}, {}, {}
    for index, window in enumerate(windows):
        signature = []
        for row in window:
            if row["id"] in owners:
                union(index, owners[row["id"]])
            owners[row["id"]] = index
            body = re.sub(r"\s+", "", message_body(row)).casefold()
            signature.append((row.get("context_role", row.get("speaker")), body))
            if len(body) >= 16:
                pieces = (
                    [body]
                    if len(body) < 64
                    else [body[start : start + 64] for start in range(0, len(body) - 63, 32)]
                )
                for piece in pieces:
                    digest = hashlib.sha256(piece.encode()).hexdigest()
                    if digest in copies:
                        union(index, copies[digest])
                    copies[digest] = index
        if len(signature) >= 2 and sum(len(body) for _, body in signature) >= 8:
            digest = hashlib.sha256(repr(signature).encode()).hexdigest()
            if digest in copies:
                union(index, copies[digest])
            copies[digest] = index
        first = window[0]
        key = (first.get("file_index"), first.get("conversation"))
        conversations.setdefault(key, []).append(index)
    for index, window in enumerate(windows):
        for row in window:
            for reference in row.get("reply_to", []):
                if reference in owners:
                    union(index, owners[reference])
    for indices in conversations.values():
        for left, right in zip(indices, indices[1:]):
            stamps = [windows[left][-1].get("timestamp"), windows[right][0].get("timestamp")]
            if (
                not all(stamps)
                or abs(
                    (
                        datetime.fromisoformat(stamps[1]) - datetime.fromisoformat(stamps[0])
                    ).total_seconds()
                )
                <= 1800
            ):
                union(left, right)
    grouped = {}
    for index, window in enumerate(windows):
        grouped.setdefault(root(index), []).extend(window)
    result = []
    for number, rows in enumerate(grouped.values()):
        unique = {row["id"]: row for row in rows}
        result.append([{**row, "dependency_group": f"g{number}"} for row in unique.values()])
    return result


def chat_probes(sources: list[dict], targets: list[str], limit: int = 2) -> list[dict]:
    """Keep window identity and user history; never expose a recorded target answer."""
    probes, context, previous_window, in_target_turn = [], [], None, False
    for row in sources:
        window = (row.get("file_index"), row.get("conversation"), row.get("window_id"))
        if window != previous_window:
            context, in_target_turn = [], False
            previous_window = window
        if row.get("speaker") in targets:
            if context and not in_target_turn:
                probes.append({"window": list(window), "context": list(context)})
                if len(probes) == limit:
                    break
            in_target_turn = True
        else:
            if in_target_turn:
                context = []
            context.append({"speaker": row.get("speaker"), "text": row["text"]})
            in_target_turn = False
    return probes


def synthetic_probes(*, regression=False) -> list[dict]:
    # Synthetic development cases are never evidence about the source person.
    if regression:
        return [
            {
                "window": ["synthetic", "disagreement", "2"],
                "context": [
                    {
                        "speaker": "other",
                        "text": "我觉得小说结局就该把一切解释清楚，读者才不用猜。你也这么想吧？",
                    }
                ],
            },
            {
                "window": ["synthetic", "disagreement", "2"],
                "context": [
                    {
                        "speaker": "other",
                        "text": "不用为了顺着我改口，也不用全盘否定，直接说你觉得怎样更合适。",
                    }
                ],
            },
            {
                "window": ["synthetic", "question", "3"],
                "context": [
                    {
                        "speaker": "other",
                        "text": "我写两个人吵架，全用对白觉得有点平。"
                        "你能具体讲讲为什么，给个小例子吗？",
                    }
                ],
            },
        ]
    return [
        {
            "window": ["synthetic", "closing", "0"],
            "context": [{"speaker": "other", "text": "我先去吃饭，晚点再说。"}],
        },
        {
            "window": ["synthetic", "support", "1"],
            "context": [{"speaker": "other", "text": "今天有点烦，暂时不想分析原因。"}],
        },
    ]


def chat_metrics(sources: list[dict], targets: list[str]) -> dict:
    rows = {row["id"]: row for row in sources if row.get("speaker") in targets}
    lengths = sorted(len(message_body(row).strip()) for row in rows.values())
    return {
        "unit": "source_message_characters_including_punctuation",
        "target_messages": len(rows),
        "p25_chars": lengths[(len(lengths) - 1) // 4] if lengths else 0,
        "median_chars": lengths[(len(lengths) - 1) // 2] if lengths else 0,
        "p90_chars": lengths[(len(lengths) - 1) * 9 // 10] if lengths else 0,
        "note": "描述已取样消息的分布，不是回复字数、条数或问句配额。",
    }


def evidence_report(findings: list[dict], sources: list[dict], targets: list[str]) -> dict:
    target_rows = {row["id"]: row for row in sources if row.get("speaker") in targets}
    scenes = {}
    evidence = []
    for finding in findings:
        rows = [target_rows[source_id] for source_id in set(finding["source_ids"])]
        groups = {row["dependency_group"] for row in rows}
        scene = finding.get("scene", "other")
        scenes.setdefault(scene, set()).update(groups)
        evidence.append(
            {
                **finding,
                "support_messages": len(rows),
                "support_groups": len(groups),
                "confidence": "multi_group_support" if len(groups) > 1 else "local_support",
                "evidence_kind": "observed_target_behavior",
            }
        )
    return {
        "evidence": evidence,
        "scene_coverage": {
            "basis": "model_classified_cited_evidence_not_all_messages",
            "observed": {scene: len(groups) for scene, groups in scenes.items()},
            "unobserved": [scene for scene in SCENES if scene not in scenes],
        },
    }


def response_diagnostics(texts: list[str]) -> dict:
    """Surface signals for review, never an authorship detector or pass/fail score."""
    starts = Counter(text.strip()[:6] for text in texts if text.strip())
    return {
        "chars": [len(text.strip()) for text in texts],
        "questions": [text.count("？") + text.count("?") for text in texts],
        "repeated_openings": sum(count - 1 for count in starts.values()),
        "note": "仅作上下文审查线索；简短、长文、礼貌、标点都不单独证明自然或AI化。",
    }
