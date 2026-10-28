"""Local-only reply windows for skill evaluation; private corpus never enters source control."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lisa.chat_corpus import decode_text, parse_chat  # noqa: E402
from scripts.prepare_real_chat_gold import EXCLUDE, REPLY, body, remove_mentions  # noqa: E402


def extract_windows(source: Path, old_directory: Path) -> dict:
    raw = source.read_bytes()
    text, _ = decode_text(raw)
    segments, people, unknown = parse_chat(text, 0)
    if unknown:
        raise ValueError("Unparsed source lines")
    labels = {p["id"]: p["label"] for p in people}
    index = defaultdict(list)
    for row in segments:
        index[(row["timestamp"][:16], labels[row["speaker"]])].append(row)
    old = [
        c
        for split in ("develop", "confirm")
        for c in json.loads((old_directory / f"{split}.json").read_text(encoding="utf-8"))["cases"]
    ]
    old_stamps = [datetime.fromisoformat(c["source"]["reply_time"]) for c in old]
    old_hours = {s.isoformat()[:13] for s in old_stamps}
    # Conservatively exclude the complete intervening window of every old pair as well.
    old_ordinals = {
        i
        for c in old
        for i in range(c["source"]["incoming_ordinal"] - 5, c["source"]["reply_ordinal"] + 6)
    }
    candidates = []
    for reply in segments:
        match = REPLY.search(reply["text"])
        if not match or reply["timestamp"][:10] < "2026-10-19":
            continue
        t = datetime.fromisoformat(reply["timestamp"])
        if t.isoformat()[:13] in old_hours:
            continue
        if min(abs((t - s).total_seconds()) for s in old_stamps) <= 1800:
            continue
        stamp = reply["timestamp"][:5] + match[1] + "T" + match[2]
        roots = [r for r in index[(stamp, match[3])] if r["ordinal"] < reply["ordinal"]]
        if match[4] != "原消息":
            quoted = remove_mentions(match[4], labels.values())
            exact = [r for r in roots if remove_mentions(body(r), labels.values()) == quoted]
            if exact:
                roots = exact
        if len(roots) != 1:
            continue
        root = roots[0]
        if root["speaker"] == reply["speaker"]:
            continue
        if not 0 <= (t - datetime.fromisoformat(root["timestamp"])).total_seconds() <= 300:
            continue
        incoming, reference = [remove_mentions(body(r), labels.values()) for r in (root, reply)]
        if not 4 <= len(incoming) <= 240 or not 1 <= len(reference) <= 200:
            continue
        if EXCLUDE.search(incoming + "\n" + reference) or "@" in incoming + reference:
            continue
        if any(label in incoming + reference for label in labels.values() if len(label) >= 3):
            continue
        nearby = segments[max(0, root["ordinal"] - 4) : reply["ordinal"] + 1]
        if any(r["ordinal"] in old_ordinals for r in nearby):
            continue
        candidates.append(
            {
                "id": "S" + str(reply["ordinal"]).zfill(5),
                "incoming": incoming,
                "reference": reference,
                "source": {
                    "incoming_ordinal": root["ordinal"],
                    "reply_ordinal": reply["ordinal"],
                    "reply_time": reply["timestamp"],
                },
                "window": [
                    {
                        "ordinal": r["ordinal"],
                        "speaker": r["speaker"],
                        "text": remove_mentions(body(r), labels.values()),
                    }
                    for r in nearby
                ],
            }
        )
    return {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "parsed_messages": len(segments),
        "prior_cases": len(old),
        "candidates": candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.resolve().relative_to(ROOT / "runtime")
    data = extract_windows(args.source, ROOT / "runtime/reports/real-chat-gold-v1")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({"candidates": len(data["candidates"]), "source_sha256": data["source_sha256"]})
    )


if __name__ == "__main__":
    main()
