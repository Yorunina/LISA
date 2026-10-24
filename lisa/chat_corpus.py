"""Offline QQ TXT indexing and bounded target-centred windows; no model or network calls."""

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATE = r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
HEADER = re.compile(rf"^\[?(?P<date>{DATE})\s+(?P<time>{TIME})\]?\s+(?P<speaker>.+?)\s*$")
REVERSE = re.compile(rf"^(?P<speaker>.+?)\s+\[?(?P<date>{DATE})\s+(?P<time>{TIME})\]?\s*$")
CONVERSATION = re.compile(r"^会话[:：]\s*(.+?)\s*$")
# The referenced exporter appends per-message seq/id/type to its speaker header.
EXPORT_METADATA = re.compile(
    r"(?: self)?(?: new_after_import_snapshot)? seq=(?:-?\d+|None) id=(?:-?\d+|None) type=\S+$"
)
EXPORTER_V5 = re.compile(r"^\[QQChatExporter V5(?:\s*/[^\n]*)?\]\s*(?:\n|$)")
V5_MESSAGE = re.compile(r"^(?P<speaker>[^\n]+):\n时间: (?P<stamp>[^\n]*)\n", re.MULTILINE)
V5_FOOTER = re.compile(
    r"^={10,}\n[ \t]*导出完成[ \t]*\n={10,}\n总计导出 \d+ 条消息\n导出时间: [^\n]+\s*\Z",
    re.MULTILINE,
)
V5_COUNT = re.compile(r"^消息总数: (\d+)[ \t]*$", re.MULTILINE)


def is_exporter_v5(text: str) -> bool:
    return EXPORTER_V5.match(text) is not None


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode strictly; mark NULs in known V5 exports without changing source offsets."""
    encodings = (
        ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    )
    for encoding in encodings:
        try:
            value = data.decode(encoding)
        except UnicodeError:
            continue
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if is_exporter_v5(value):
            value = value.replace("\x00", "\ufffd")
        invalid = next((c for c in value if ord(c) < 32 and c not in "\n\r\t"), None)
        if invalid is not None:
            raise ValueError(
                f"文本含不支持的控制字符 U+{ord(invalid):04X}，请检查文件内容后重新上传。"
            )
        return value, encoding
    raise ValueError("无法识别文本编码，请转换为 UTF-8 TXT/Markdown 后重新上传。")


def _parse_exporter_v5(text: str, file_index: int) -> tuple[list[dict], list[dict], int]:
    # Select this parser by the export signature; never interpret 时间: as a nickname.
    footer = V5_FOOTER.search(text)
    end = footer.start() if footer else len(text)
    matches = list(V5_MESSAGE.finditer(text, 0, end))
    segments, people, identities, unknown = [], {}, {}, 0
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else end
        body = text[match.end() : stop]
        try:
            timestamp = datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S").isoformat()
        except ValueError:
            timestamp = None
        if timestamp is None or not body.startswith("内容:") or not match["speaker"].strip():
            unknown += sum(bool(line.strip()) for line in text[match.start() : stop].splitlines())
            continue
        label = match["speaker"].strip()
        speaker = identities.setdefault(label, f"{file_index}:p{len(identities)}")
        people.setdefault(speaker, {"id": speaker, "label": label, "messages": 0})
        people[speaker]["messages"] += 1
        segments.append(
            {
                "id": f"{file_index}:{index}",
                "start": match.start(),
                "end": stop,
                "text": text[match.start() : stop],
                "speaker": speaker,
                "conversation": f"{file_index}:c0",
                "timestamp": timestamp,
                "ordinal": index,
            }
        )
    return segments, list(people.values()), unknown


def json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def parse_chat(text: str, file_index: int) -> tuple[list[dict], list[dict], int]:
    if is_exporter_v5(text):
        return _parse_exporter_v5(text, file_index)
    segments, people, identities, conversations = [], {}, {}, {}
    current, conversation, offset, unknown = None, f"{file_index}:c0", 0, 0

    def finish(end):
        if current is not None:
            current["end"] = end
            current["text"] = text[current["start"] : end]
            segments.append(current)

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        boundary = CONVERSATION.match(stripped)
        match = HEADER.match(stripped) or REVERSE.match(stripped)
        if boundary:
            finish(offset)
            current = None
            conversation = conversations.setdefault(
                boundary[1], f"{file_index}:c{len(conversations) + 1}"
            )
        elif match:
            finish(offset)
            label = EXPORT_METADATA.sub("", match["speaker"]).strip()
            speaker = identities.setdefault(label, f"{file_index}:p{len(identities)}")
            people.setdefault(speaker, {"id": speaker, "label": label, "messages": 0})
            people[speaker]["messages"] += 1
            stamp = f"{match['date'].replace('/', '-')} {match['time']}"
            try:
                timestamp = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").isoformat()
            except ValueError:
                try:
                    timestamp = datetime.strptime(stamp, "%Y-%m-%d %H:%M").isoformat()
                except ValueError:
                    timestamp = None
            current = {
                "id": f"{file_index}:{len(segments)}",
                "start": offset,
                "speaker": speaker,
                "conversation": conversation,
                "timestamp": timestamp,
                "ordinal": len(segments),
            }
        elif current is None and stripped:
            unknown += 1
        offset += len(line)
    finish(len(text))
    return segments, list(people.values()), unknown


def _spread(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]
    return [items[i * (len(items) - 1) // (limit - 1)] for i in range(limit)]


def target_windows(
    segments: list[dict],
    targets: list[str],
    *,
    before=4,
    after=2,
    max_messages=24,
    max_bytes=16_000,
    max_windows=24,
    max_gap_seconds=1800,
) -> tuple[list[list[dict]], dict]:
    """Index all metadata locally; send only sampled, intact, conversation-scoped messages.

    Counts/time are retrieval bounds, not a claim that a window is semantically complete.
    Adjacent windows may overlap; source IDs remain unchanged for dependency grouping.
    """
    if not (
        0 <= before <= 50
        and 0 <= after <= 50
        and 1 <= max_messages <= 100
        and max_windows >= 1
        and max_bytes >= 512
        and max_gap_seconds > 0
    ):
        raise ValueError("Invalid chat window limits")
    target_set = set(targets)
    groups = defaultdict(list)
    run, previous_key, previous_ordinal = 0, None, None
    for item in segments:
        key = (item.get("file_index", 0), item.get("conversation", "default"))
        ordinal = item.get("ordinal")
        if key != previous_key or (
            ordinal is not None and previous_ordinal is not None and ordinal != previous_ordinal + 1
        ):
            run += 1
        groups[(*key, run)].append(item)
        previous_key, previous_ordinal = key, ordinal
    candidates, oversized = [], 0
    for group in groups.values():
        cursor = 0
        for index, item in enumerate(group):
            if index < cursor or item.get("speaker") not in target_set:
                continue
            anchor = item
            left = right = index

            def nearby(other):
                if not anchor.get("timestamp") or not other.get("timestamp"):
                    return True
                delta = datetime.fromisoformat(other["timestamp"]) - datetime.fromisoformat(
                    anchor["timestamp"]
                )
                return abs(delta.total_seconds()) <= max_gap_seconds

            # Prefer an intact consecutive target turn, keeping its original message split.
            while (
                right + 1 < len(group)
                and group[right + 1].get("speaker") in target_set
                and right - left + 1 < max_messages
                and nearby(group[right + 1])
            ):
                right += 1
                if json_bytes(group[left : right + 1]) > max_bytes - 512:
                    right -= 1
                    break
            for _ in range(before):
                if left == 0 or right - left + 1 >= max_messages or not nearby(group[left - 1]):
                    break
                if json_bytes(group[left - 1 : right + 1]) > max_bytes - 512:
                    break
                left -= 1
            for _ in range(after):
                if (
                    right + 1 == len(group)
                    or right - left + 1 >= max_messages
                    or not nearby(group[right + 1])
                ):
                    break
                if json_bytes(group[left : right + 2]) > max_bytes - 512:
                    break
                right += 1
            window_id = f"w{len(candidates)}"
            window = [
                {
                    **row,
                    "window_id": window_id,
                    "context_role": "target" if row.get("speaker") in target_set else "context",
                }
                for row in group[left : right + 1]
            ]
            # The role/window metadata is part of the actual serialized input budget.
            while len(window) > 1 and json_bytes(window) > max_bytes:
                if window[0]["id"] != anchor["id"]:
                    window.pop(0)
                else:
                    window.pop()
            if json_bytes(window) > max_bytes:
                oversized += 1
                continue
            candidates.append(window)
            # Later target messages already covered by this window are not new anchors.
            included_targets = {row["id"] for row in window if row["speaker"] in target_set}
            cursor = (
                max(i for i in range(index, right + 1) if group[i]["id"] in included_targets) + 1
            )
    # Allocate across conversations first, then across their time-ordered windows.
    by_conversation = defaultdict(list)
    for window in candidates:
        row = window[0]
        by_conversation[(row.get("file_index", 0), row.get("conversation", "default"))].append(
            window
        )
    buckets = _spread(list(by_conversation.values()), max_windows)
    quotas = [0] * len(buckets)
    remaining = max_windows
    while remaining and any(quotas[i] < len(bucket) for i, bucket in enumerate(buckets)):
        for i, bucket in enumerate(buckets):
            if remaining and quotas[i] < len(bucket):
                quotas[i] += 1
                remaining -= 1
    selected = [
        window for bucket, quota in zip(buckets, quotas) for window in _spread(bucket, quota)
    ]
    selected.sort(key=lambda window: int(window[0]["window_id"][1:]))
    unique = {row["id"]: row for window in selected for row in window}
    analyzed_targets = sum(row.get("speaker") in target_set for row in unique.values())
    total_targets = sum(row.get("speaker") in target_set for row in segments)
    return selected, {
        "method": "target_windows_v1",
        "candidate_windows": len(candidates),
        "sampled_windows": len(selected),
        "total_target_messages": total_targets,
        "analyzed_target_messages": analyzed_targets,
        "context_messages": len(unique) - analyzed_targets,
        "excluded_messages": len(segments) - len(unique),
        "oversized_targets": oversized,
        "conversations": len(by_conversation),
        "sampled_conversations": len(buckets),
        "before": before,
        "after": after,
        "max_messages": max_messages,
        "max_window_bytes": max_bytes,
        "max_windows": max_windows,
        "max_gap_seconds": max_gap_seconds,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs", nargs="+", type=Path, help="QQ TXT files; encrypted BAK is not TXT"
    )
    parser.add_argument(
        "--target", action="append", default=[], help="Exact participant ID from index"
    )
    parser.add_argument("--out", type=Path, required=True, help="New private output directory")
    parser.add_argument("--max-windows", type=int, default=24)
    parser.add_argument("--before", type=int, default=4)
    parser.add_argument("--after", type=int, default=2)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output directory already exists; choose a new private directory")
    files, participants, unknown, replaced_nulls = [], [], 0, 0
    for index, path in enumerate(args.inputs):
        if path.suffix.lower() != ".txt":
            parser.error("Only readable TXT is supported; encrypted .bak needs a verified decoder")
        raw = path.read_bytes()
        try:
            text, encoding = decode_text(raw)
        except ValueError as error:
            parser.error(str(error))
        replaced_nulls += raw.decode(encoding).count("\x00")
        messages, people, missing = parse_chat(text, index)
        files.extend({**row, "file": path.name, "file_index": index} for row in messages)
        participants.extend(people)
        unknown += missing
    known = {person["id"] for person in participants}
    if set(args.target) - known:
        parser.error("Unknown target ID; first run without --target to inspect index.json")
    windows, coverage = (
        target_windows(
            files, args.target, before=args.before, after=args.after, max_windows=args.max_windows
        )
        if args.target
        else ([], {})
    )
    args.out.mkdir(parents=True)
    index = {
        "participants": participants,
        "messages": len(files),
        "unrecognized_lines": unknown,
        "replaced_nulls": replaced_nulls,
        "coverage": coverage,
        "validation": "not_executed",
        "raw_messages_in_index": False,
    }
    (args.out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.target:
        with (args.out / "windows.jsonl").open("w", encoding="utf-8") as output:
            for window in windows:
                output.write(
                    json.dumps(
                        {"window_id": window[0]["window_id"], "messages": window},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    print(
        json.dumps(
            {
                "messages": len(files),
                "participants": len(participants),
                "sampled_windows": len(windows),
                "validation": "not_executed",
            }
        )
    )


if __name__ == "__main__":
    main()
