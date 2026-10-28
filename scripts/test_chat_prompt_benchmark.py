"""Offline integrity checks for prompt evaluation and clustered confirmation."""

import unittest

from scripts.benchmark_chat_prompt import group_summary, messages_for, summarize


def score(naturalness, passed=True):
    return {
        "naturalness": naturalness,
        "personality": 3,
        "counseling": False,
        "unasked_advice": False,
        "question_pressure": False,
        "fabrication": False,
        "rubric_met": passed,
        "reason": "test",
    }


class PromptBenchmarkChecks(unittest.TestCase):
    def test_generation_never_sees_reference_or_grading_fields(self):
        case = {
            "reference": "SECRET_REFERENCE",
            "expected": "SECRET_EXPECTED",
            "history": [{"role": "assistant", "content": "already sent"}],
        }
        result = messages_for(case, [], "actual incoming message")
        self.assertEqual(
            result, [case["history"][0], {"role": "user", "content": "actual incoming message"}]
        )

    def test_replicates_are_averaged_within_case_before_inference(self):
        rows = [
            {
                "id": str(i),
                "case_id": case,
                "baseline": score(2),
                "candidate": score(2 + delta),
                "critical": False,
                "variant": "candidate",
                "winner": "tie",
            }
            for i, (case, delta) in enumerate([("x", 2), ("x", 0), ("y", -1)])
        ]
        result = group_summary(rows)
        self.assertEqual(result["independent_cases"], 2)
        self.assertEqual(result["pairs_scored"], 3)
        self.assertEqual(result["mean_delta"], 0)
        self.assertEqual(result["case_mean_deltas"], {"x": 1, "y": -1})

    def make_confirmation(self):
        report = {
            "stage": "confirm",
            "pairs": [],
            "calls": [],
            "judgments": [],
            "calibration": {"passed": True},
        }
        for case in range(12):
            for rep in range(2):
                key = f"H{case}/r{rep}"
                report["pairs"].append(
                    {
                        "id": key,
                        "case": {"id": f"H{case}", "critical": case < 6},
                        "rep": rep,
                        "group": "confirmation",
                        "variant": "candidate",
                        "outputs": {"baseline": {}, "candidate": {}},
                    }
                )
                judgment = {
                    "role": "primary",
                    "mapping": {key: {"a": "candidate", "b": "baseline"}},
                    "scores": [{"case_id": key, "a": score(4), "b": score(1), "winner": "a"}],
                }
                report["judgments"].append(judgment)
                if rep == 0:
                    report["judgments"].append({**judgment, "role": "reversed"})
        return report

    def test_strong_complete_confirmation_can_pass(self):
        result = summarize(self.make_confirmation())
        self.assertTrue(result["criteria_met"])
        self.assertEqual(result["groups"]["confirmation:candidate"]["independent_cases"], 12)

    def test_missing_critical_content_blocks_naturalness_success(self):
        report = self.make_confirmation()
        changed = 0
        for batch in report["judgments"]:
            if batch["role"] == "primary" and changed < 2:
                batch["scores"][0]["a"]["rubric_met"] = False
                changed += 1
        result = summarize(report)
        self.assertFalse(result["criteria_met"])
        self.assertFalse(result["confirmation_gates"]["critical_behavior"])
        self.assertTrue(result["confirmation_gates"]["case_significance"])

    def test_missing_pairs_cannot_be_counted_as_success(self):
        report = self.make_confirmation()
        skipped = 0
        remaining = []
        for batch in report["judgments"]:
            if batch["role"] == "primary" and skipped < 3:
                skipped += 1
                continue
            remaining.append(batch)
        report["judgments"] = remaining
        result = summarize(report)
        self.assertFalse(result["criteria_met"])
        self.assertFalse(result["confirmation_gates"]["coverage"])

    def test_development_selection_uses_paired_gain_not_raw_score(self):
        report = {"stage": "develop", "pairs": [], "calls": [], "judgments": []}
        for variant, base, candidate in (("candidate_a", 0, 3), ("candidate_b", 3, 4)):
            for case in range(6):
                key = f"D{case}/{variant}"
                report["pairs"].append(
                    {
                        "id": key,
                        "case": {"id": f"D{case}"},
                        "rep": 0,
                        "group": "development",
                        "variant": variant,
                        "outputs": {"baseline": {}, variant: {}},
                    }
                )
                report["judgments"].append(
                    {
                        "role": "primary",
                        "mapping": {key: {"a": variant, "b": "baseline"}},
                        "scores": [
                            {"case_id": key, "a": score(candidate), "b": score(base), "winner": "a"}
                        ],
                    }
                )
        self.assertEqual(summarize(report)["selected_variant"], "candidate_a")


if __name__ == "__main__":
    unittest.main()
