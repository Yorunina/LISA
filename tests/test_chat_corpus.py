"""Synthetic target-window invariants, including the standalone skill tool."""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from lisa.chat_corpus import json_bytes, parse_chat, target_windows
from lisa.style_distillation import build_plan


def messages(count=1000):
    start = datetime(2026, 1, 1)
    return [
        {
            "id": f"0:{i}",
            "ordinal": i,
            "file_index": 0,
            "file": "group.txt",
            "conversation": "0:c0",
            "speaker": "target" if i % 100 == 50 else "other",
            "timestamp": (start + timedelta(minutes=i)).isoformat(),
            "text": f"合成消息 {i}，保留分条。\n",
        }
        for i in range(count)
    ]


def test_export_metadata_does_not_split_identity_and_conversations_stay_distinct():
    text = (
        "会话：群甲\n[2026-01-01 10:00:00] 甲(100001) seq=1 id=10 type=text\n甲说话。\n"
        "[2026-01-01 10:01:00] 甲(100001) self seq=2 id=11 type=text\n继续分条。\n"
        "会话：私聊乙\n2026-01-01 10:01:01 乙(100002)\n不相关的会话。\n"
    )
    rows, people, unknown = parse_chat(text, 0)
    assert unknown == 0 and len(people) == 2
    assert rows[0]["speaker"] == rows[1]["speaker"]
    assert people[0]["label"] == "甲(100001)" and people[0]["messages"] == 2
    assert rows[1]["conversation"] != rows[2]["conversation"]
    windows, counts = target_windows(rows, [people[0]["id"]])
    assert len(windows) == 1 and counts["analyzed_target_messages"] == 2
    assert [row["text"] for row in windows[0]] == [row["text"] for row in rows[:2]]


def test_large_group_is_bounded_and_sampled_across_time_not_read_in_full():
    rows = messages(5000)
    windows, counts = target_windows(rows, ["target"], max_windows=3)
    assert counts["total_target_messages"] == 50
    assert counts["analyzed_target_messages"] == 3
    assert counts["excluded_messages"] > 4900
    assert len(windows) == 3 and windows[0][0]["ordinal"] < 100
    assert windows[-1][-1]["ordinal"] > 4900
    for window in windows:
        assert len(window) <= 24 and json_bytes(window) <= 16_000
        assert any(row["context_role"] == "target" for row in window)
        assert any(row["context_role"] == "context" for row in window)
        assert all(row["text"] == rows[row["ordinal"]]["text"] for row in window)


def test_selection_gaps_time_gaps_and_foreign_chats_do_not_become_neighbors():
    rows = messages(60)
    rows[49]["timestamp"] = "2025-12-01T00:00:00"
    rows[51]["conversation"] = "foreign"
    selected = [rows[0], *rows[49:53]]
    windows, _ = target_windows(selected, ["target"], max_windows=3)
    ids = {row["id"] for window in windows for row in window}
    assert ids == {"0:50"}


def test_oversized_target_is_reported_without_truncating_or_hiding_later_targets():
    rows = messages(160)
    rows[50]["text"] = "完整长消息" * 1000
    windows, counts = target_windows(rows, ["target"], max_bytes=1000)
    assert counts["oversized_targets"] == 1
    assert counts["total_target_messages"] == 2
    assert counts["analyzed_target_messages"] == 1
    assert all(row["id"] != "0:50" for window in windows for row in window)
    assert any(row["id"] == "0:150" for window in windows for row in window)


def test_plan_excludes_remote_group_chatter_and_preserves_window_budget(settings):
    rows = messages(3000)
    # A deliberately large amount of unrelated text must not inflate model context/calls.
    for row in rows:
        if row["speaker"] != "target" and row["ordinal"] % 100 not in range(46, 53):
            row["text"] = "完全无关的群聊长消息。" * 100
    text = "".join(row["text"] for row in rows)
    payload = {"files": [{"name": "group.txt", "text": text, "segments": rows}]}
    plan = build_plan(payload, "personality", ["target"], [], settings)
    sources = [row for batch in plan.batches for row in batch]
    assert all("完全无关" not in row["text"] for row in sources)
    assert plan.calls <= settings.style_distillation_max_calls
    assert plan.coverage["analyzed_segments"] < plan.coverage["selected_segments"]
    assert plan.coverage["sampling"]["analyzed_target_messages"] <= 13
    assert all(
        json_bytes(batch) <= settings.style_distillation_chunk_bytes for batch in plan.batches
    )


def test_standalone_tool_indexes_without_content_and_exports_only_selected_windows(tmp_path):
    source = tmp_path / "synthetic.txt"
    text = "".join(
        f"2026-01-01 10:{i // 60:02}:{i % 60:02} {'甲' if i == 50 else '乙'}\n合成私密消息{i}。\n"
        for i in range(100)
    )
    source.write_text(text, encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "lisa" / "chat_corpus.py"
    index_dir, window_dir = tmp_path / "index", tmp_path / "windows"
    run = subprocess.run(
        [sys.executable, str(script), str(source), "--out", str(index_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "合成私密" not in run.stdout
    index = json.loads((index_dir / "index.json").read_text(encoding="utf-8"))
    target = next(person["id"] for person in index["participants"] if person["label"] == "甲")
    subprocess.run(
        [sys.executable, str(script), str(source), "--target", target, "--out", str(window_dir)],
        capture_output=True,
        check=True,
    )
    assert "合成私密" not in (window_dir / "index.json").read_text(encoding="utf-8")
    windows = [
        json.loads(line)
        for line in (window_dir / "windows.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(windows) == 1 and len(windows[0]["messages"]) == 7
    assert source.read_text(encoding="utf-8") == text
    refusal = subprocess.run(
        [
            sys.executable,
            str(script),
            str(tmp_path / "encrypted.bak"),
            "--out",
            str(tmp_path / "bak"),
        ],
        capture_output=True,
    )
    assert refusal.returncode != 0 and not (tmp_path / "bak").exists()
