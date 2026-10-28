"""Local-only V5 reply extraction. Private messages and provenance stay in runtime/."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lisa.chat_corpus import decode_text, parse_chat  # noqa: E402

REPLY = re.compile(r"^回复: (\d{2}-\d{2}) (\d{2}:\d{2}) (.+?) - (.*)$", re.MULTILINE)
META = re.compile(r"\n(?:资源|提及|回复):")
EXCLUDE = re.compile(
    r"\[(?:图片|表情|视频|文件|语音|转发)|https?://|www\.|\b\d{6,}\b|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|身份证|手机号|住址|自杀|想死|"
    r"萝莉|正太|大唧|吉吉|鸡鸡|计计|裸照|色情|强奸|傻[逼B]|操你|你妈|"
    r"(?:忽略|无视).{0,8}(?:指令|提示)|system prompt|api.?key",
    re.IGNORECASE,
)


def body(segment):
    value = segment["text"].split("内容:", 1)[1].lstrip()
    return META.split(value, 1)[0].replace("[回复消息]", "").strip()


def remove_mentions(text, labels):
    for label in sorted(labels, key=len, reverse=True):
        text = text.replace("@" + label, "")
    return re.sub(r"[ \t]+", " ", text).strip()


def extract(source):
    raw = source.read_bytes()
    text, encoding = decode_text(raw)
    segments, people, unknown = parse_chat(text, 0)
    labels = {p["id"]: p["label"] for p in people}
    index = defaultdict(list)
    for m in segments:
        index[(m["timestamp"][:16], labels[m["speaker"]])].append(m)
    reasons, candidates = Counter(), []
    for m in segments:
        match = REPLY.search(m["text"])
        if not match:
            reasons["no_explicit_reply"] += 1
            continue
        stamp = m["timestamp"][:5] + match[1] + "T" + match[2]
        roots = [x for x in index[(stamp, match[3])] if x["ordinal"] < m["ordinal"]]
        if match[4] != "原消息":
            quoted = remove_mentions(match[4], labels.values())
            exact = [x for x in roots if remove_mentions(body(x), labels.values()) == quoted]
            if exact:
                roots = exact
        if len(roots) != 1:
            reasons["unresolved_or_ambiguous_reply"] += 1
            continue
        root = roots[0]
        gap = (
            datetime.fromisoformat(m["timestamp"]) - datetime.fromisoformat(root["timestamp"])
        ).total_seconds()
        if not 0 <= gap <= 300 or m["speaker"] == root["speaker"]:
            reasons["gap_or_self_reply"] += 1
            continue
        incoming, reference = (remove_mentions(body(x), labels.values()) for x in (root, m))
        if not (6 <= len(incoming) <= 240 and 4 <= len(reference) <= 200):
            reasons["length"] += 1
            continue
        if EXCLUDE.search(incoming + "\n" + reference) or "@" in incoming + reference:
            reasons["media_sensitive_or_unresolved_mention"] += 1
            continue
        if any(label in incoming + reference for label in labels.values() if len(label) >= 3):
            reasons["embedded_nickname"] += 1
            continue
        day = m["timestamp"][:10]
        split = "train" if day <= "2026-10-18" else "develop" if day <= "2026-10-20" else "confirm"
        candidates.append(
            {
                "id": "Q" + str(m["ordinal"]).zfill(5),
                "split": split,
                "day": day,
                "incoming": incoming,
                "reference": reference,
                "source": {
                    "incoming_ordinal": root["ordinal"],
                    "reply_ordinal": m["ordinal"],
                    "incoming_speaker": root["speaker"],
                    "reply_speaker": m["speaker"],
                    "incoming_time": root["timestamp"],
                    "reply_time": m["timestamp"],
                    "root_chars": [root["start"], root["end"]],
                    "reply_chars": [m["start"], m["end"]],
                },
                "nearby_ordinals": list(
                    range(max(0, root["ordinal"] - 3), min(len(segments), m["ordinal"] + 2))
                ),
            }
        )
    return candidates, {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_bytes": len(raw),
        "encoding": encoding,
        "parsed_messages": len(segments),
        "participants": len(people),
        "unknown_lines": unknown,
        "messages_by_day": dict(Counter(x["timestamp"][:10] for x in segments)),
        "candidate_counts": dict(Counter(x["split"] for x in candidates)),
        "rejections": dict(reasons),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to(ROOT / "runtime")
    output.mkdir(parents=True, exist_ok=True)
    candidates, stats = extract(args.source)
    (output / "candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "extraction.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
