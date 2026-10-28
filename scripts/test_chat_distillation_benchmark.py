"""Offline checks for score attribution, missing outcomes and held-out data isolation."""

import copy
import json
import unittest

from lisa.settings import Settings
from scripts.benchmark_chat_distillation import (
    DATASET,
    Judgments,
    calibration_case,
    checked_judgments,
    summarize,
    validate_dataset,
)


class BenchmarkChecks(unittest.TestCase):
    def score(self, naturalness):
        return {
            "naturalness": naturalness,
            "personality": 2,
            "counseling": False,
            "unasked_advice": False,
            "question_pressure": False,
            "fabrication": False,
            "rubric_met": True,
            "reason": "具体回应",
        }

    def test_missing_cases_remain_in_scheduled_denominator(self):
        pair = {
            "case_id": "x",
            "a": self.score(1),
            "b": self.score(3),
            "winner": "b",
            "rationale": "b更贴合来话",
        }
        report = {
            "cases": [{"id": "x"}, {"id": "missing"}],
            "calls": [],
            "judgments": [
                {
                    "role": "primary",
                    "mapping": {"x": {"a": "baseline", "b": "distilled"}},
                    "scores": [pair],
                }
            ],
        }
        result = summarize(report)
        self.assertEqual(result["scheduled_pairs"], 2)
        self.assertEqual(result["scored_pairs"], 1)
        self.assertEqual(result["unscored_pairs"], 1)
        self.assertEqual(result["wins"]["distilled"], 1)
        self.assertEqual(result["paired_naturalness_delta"], 2)

    def test_duplicate_judge_ids_cannot_silently_weight_a_case_twice(self):
        pair = {
            "case_id": "x",
            "a": self.score(1),
            "b": self.score(3),
            "winner": "b",
            "rationale": "b更自然",
        }
        result = Judgments(cases=[pair, pair])
        with self.assertRaisesRegex(ValueError, "judge_case_mismatch"):
            checked_judgments(result, ["x", "y"])

    def test_dataset_partitions_are_independent(self):
        data = json.loads(DATASET.read_text(encoding="utf-8"))
        result = validate_dataset(data, Settings())
        self.assertEqual(set(result), {"dry", "warm"})
        self.assertTrue(all(r["sessions"] == [5, 1, 1] for r in result.values()))

    def test_reference_copied_from_source_is_rejected(self):
        data = copy.deepcopy(json.loads(DATASET.read_text(encoding="utf-8")))
        data["cases"][0]["reference"] = data["personas"]["dry"]["sessions"][5][0][1]
        with self.assertRaisesRegex(AssertionError, "external_test_source_overlap"):
            validate_dataset(data, Settings())

    def test_calibration_follows_revised_context_and_reference(self):
        anchor = {
            "case_id": "new",
            "context": "车坐过站了。",
            "better": "可以逛一逛。",
            "worse": "我们先接纳内心。",
        }
        original = copy.deepcopy(anchor)
        sample = calibration_case(anchor)
        self.assertEqual(sample["context"], [anchor["context"]])
        self.assertEqual(sample["reference"], anchor["better"])
        self.assertNotIn("不提供办法", sample["expected"])
        self.assertEqual(anchor, original)

    def test_explicit_anchor_requirement_is_not_replaced(self):
        anchor = {
            "case_id": "x",
            "context": "x",
            "better": "a",
            "worse": "b",
            "expected": "优先看说话是否顺口。",
        }
        self.assertEqual(calibration_case(anchor)["expected"], anchor["expected"])


if __name__ == "__main__":
    unittest.main()
