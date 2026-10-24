"""Evidence grouping, blind probes and regression boundaries, using synthetic text only."""

from lisa.chat_style import chat_metrics, chat_probes, dependency_groups, evidence_report
from lisa.style_corpus import parse_chat
from lisa.style_distillation import build_plan


def row(message, window, speaker="other", **extra):
    return {
        "id": message,
        "file_index": 0,
        "conversation": "c0",
        "window_id": window,
        "speaker": speaker,
        "text": f"synthetic {message}",
        "timestamp": f"2026-10-{20 + int(window[-1])}T12:00:00",
        **extra,
    }


def test_probes_include_independent_windows_and_do_not_leak_target_answers():
    rows = [
        row("q1", "w0"),
        row("a1", "w0", "target", text="hidden answer one"),
        row("q2", "w1"),
        row("a2", "w1", "target", text="hidden answer two"),
    ]
    probes = chat_probes(rows, ["target"])
    assert len(probes) == 2 and probes[0]["window"] != probes[1]["window"]
    assert [probe["context"][0]["text"] for probe in probes] == ["synthetic q1", "synthetic q2"]
    assert "hidden" not in str(probes)


def test_overlap_copies_and_explicit_replies_cannot_cross_dependency_groups():
    duplicate = "a substantial copied message that must not become independent evidence"
    windows = [
        [row("q1", "w0"), row("a1", "w0", "target", text=duplicate)],
        [row("q2", "w1", reply_to=["a1"]), row("a2", "w1", "target")],
        [row("q3", "w2"), row("a3", "w2", "target", text=duplicate)],
        [row("q4", "w3"), row("a4", "w3", "target")],
    ]
    groups = dependency_groups(windows)
    assert len(groups) == 2
    assert {message["id"] for message in groups[0]} == {"q1", "a1", "q2", "a2", "q3", "a3"}
    evidence = evidence_report(
        [{"source_ids": ["a1", "a2", "a3"], "scene": "casual"}], groups[0], ["target"]
    )
    assert evidence["evidence"][0]["support_messages"] == 3
    assert evidence["evidence"][0]["support_groups"] == 1


def test_single_export_can_have_disjoint_development_and_holdout(settings):
    text = "".join(
        f"2026-10-{day} 12:00:00 甲\nquestion-{day}\n2026-10-{day} 12:00:01 乙\nreply-{day}\n"
        for day in range(20, 24)
    )
    rows, _, _ = parse_chat(text, 0)
    plan = build_plan(
        {"files": [{"name": "synthetic.txt", "text": text, "segments": rows}]},
        "personality",
        ["0:p1"],
        [],
        settings,
    )
    assert plan.development and plan.holdout
    train = {row["id"] for batch in plan.batches for row in batch}
    dev = {row["id"] for row in plan.development}
    test = {row["id"] for row in plan.holdout}
    assert not train & dev and not train & test and not dev & test
    assert train | dev | test == {row["id"] for row in rows}
    assert plan.calls <= settings.style_distillation_max_calls
    assert plan.coverage["files"][0]["selected_segments"] == 8


def test_metrics_measure_message_body_rather_than_exporter_header():
    metrics = chat_metrics(
        [
            row(
                "1", "w0", "target", text="某人(700000001):\n时间: 2026-10-20 12:00:00\n内容: 好\n"
            ),
            row("2", "w0", "other", text="a long context that is not the target's speech"),
        ],
        ["target"],
    )
    assert metrics["target_messages"] == 1 and metrics["median_chars"] == 1
