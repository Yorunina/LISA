import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lisa.model_calls import ModelCallUnknown
from lisa.settings import Settings
from lisa.style_distillation import TrialOutput
from scripts.benchmark_chat_prompt import LiveRun, messages_for
from scripts.benchmark_real_chat_prompt import (
    GENERATION_FORMAT,
    RealRun,
    cluster_summary,
    generate_for_benchmark,
    validate_data,
)
from scripts.prepare_real_chat_gold import extract


def source(*messages):
    return "[QQChatExporter V5 / local-test]\n" + "\n\n".join(
        f"{name}:\n时间: 2026-10-20 {stamp}\n内容: {text}" for name, stamp, text in messages
    )


class RealChatBenchmarkTests(unittest.TestCase):
    def extract_text(self, text):
        return extract(Mock(read_bytes=Mock(return_value=text.encode("utf-8"))))

    def test_explicit_reply_resolves_and_removes_mentions(self):
        rows, stats = self.extract_text(
            source(
                ("成员甲", "10:00:01", "今天终于弄明白这个问题了"),
                (
                    "成员乙",
                    "10:00:20",
                    "[回复消息]@成员甲 那就可以下班了\n提及: 成员甲\n"
                    "回复: 10-20 10:00 成员甲 - 原消息",
                ),
            )
        )
        self.assertEqual(stats["parsed_messages"], 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference"], "那就可以下班了")
        self.assertLess(rows[0]["source"]["incoming_ordinal"], rows[0]["source"]["reply_ordinal"])

    def test_ambiguous_minute_not_silently_paired(self):
        rows, stats = self.extract_text(
            source(
                ("成员甲", "10:00:01", "第一件事已经弄明白了"),
                ("成员甲", "10:00:04", "第二件事也已经处理好了"),
                (
                    "成员乙",
                    "10:00:20",
                    "[回复消息]那就可以下班了\n回复: 10-20 10:00 成员甲 - 原消息",
                ),
            )
        )
        self.assertEqual(rows, [])
        self.assertEqual(stats["rejections"]["unresolved_or_ambiguous_reply"], 1)

    def test_quote_disambiguates_same_minute(self):
        rows, _ = self.extract_text(
            source(
                ("成员甲", "10:00:01", "第一件事已经弄明白了"),
                ("成员甲", "10:00:04", "第二件事也已经处理好了"),
                (
                    "成员乙",
                    "10:00:20",
                    "[回复消息]那就可以下班了\n回复: 10-20 10:00 成员甲 - 第一件事已经弄明白了",
                ),
            )
        )
        self.assertEqual(rows[0]["source"]["incoming_ordinal"], 0)

    def test_media_and_sensitive_details_excluded(self):
        for incoming in ("看看这张[图片:foo.jpg]", "我的手机号是13812345678"):
            with self.subTest(incoming=incoming):
                rows, _ = self.extract_text(
                    source(
                        ("成员甲", "10:00:01", incoming),
                        (
                            "成员乙",
                            "10:00:20",
                            "[回复消息]那就可以下班了\n回复: 10-20 10:00 成员甲 - 原消息",
                        ),
                    )
                )
                self.assertEqual(rows, [])

    def test_adjacent_unlinked_messages_not_gold(self):
        rows, _ = self.extract_text(
            source(
                ("成员甲", "10:00:01", "今天终于弄明白这个问题了"),
                ("成员乙", "10:00:20", "那就可以下班了"),
            )
        )
        self.assertEqual(rows, [])

    def test_reference_not_in_generation_messages(self):
        case = {
            "turns": ["实际来话"],
            "reference": "绝不能泄漏的下一条",
            "expected": "隐藏要求",
            "source": {"private": "隐藏映射"},
        }
        self.assertEqual(
            messages_for(case, [], case["turns"][0]), [{"role": "user", "content": "实际来话"}]
        )

    def test_shared_hour_is_one_cluster(self):
        rows, pairs = [], []
        for cid, delta in (("a", 2), ("b", 0), ("c", -1)):
            rows.append(
                {
                    "id": cid,
                    "case_id": cid,
                    "baseline": {"naturalness": 2, "rubric_met": True, "fabrication": False},
                    "candidate": {
                        "naturalness": 2 + delta,
                        "rubric_met": True,
                        "fabrication": False,
                    },
                    "critical": False,
                    "auto_checks": {},
                    "variant": "candidate",
                    "winner": "tie",
                }
            )
            pairs.append(
                {"id": cid, "case": {"id": cid, "episode": "hour1" if cid != "c" else "hour2"}}
            )
        stats = cluster_summary(rows, pairs)
        self.assertEqual(stats["independent_cases"], 2)
        self.assertEqual(stats["mean_delta"], 0)

    def test_cross_split_source_overlap_rejected(self):
        def data(n, day, offset):
            return {
                "cases": [
                    {
                        "id": str(i),
                        "reference": "参考",
                        "turns": ["来话"],
                        "expected": "要求",
                        "source": {
                            "incoming_ordinal": offset + 2 * i,
                            "reply_ordinal": offset + 2 * i + 1,
                            "incoming_speaker": "a",
                            "reply_speaker": "b",
                            "reply_time": day + "T10:00:00",
                        },
                    }
                    for i in range(n)
                ]
            }

        dev, hold = data(12, "2026-10-20", 0), data(24, "2026-10-21", 100)
        self.assertEqual(validate_data(dev, hold)["source_overlap"], 0)
        bad = copy.deepcopy(hold)
        bad["cases"][0]["source"]["incoming_ordinal"] = 0
        with self.assertRaises(AssertionError):
            validate_data(dev, bad)


class OutageCircuitTests(unittest.IsolatedAsyncioTestCase):
    def run_object(self):
        run = object.__new__(RealRun)
        run.report = {}
        run.reused_baseline = None
        run.consecutive_upstream_errors = 0
        run.save = Mock()
        return run

    async def test_two_unknowns_stop_before_third_provider_call(self):
        run = self.run_object()
        with patch.object(LiveRun, "call", new_callable=AsyncMock) as provider:
            provider.side_effect = ModelCallUnknown("unknown")
            for _ in range(2):
                with self.assertRaises(ModelCallUnknown):
                    await run.call("probe", "instructions", [])
            with self.assertRaisesRegex(RuntimeError, "upstream_circuit_open"):
                await run.call("probe", "instructions", [])
            self.assertEqual(provider.await_count, 2)
            self.assertTrue(run.report["upstream_circuit_open"])

    async def test_success_resets_consecutive_failure_count(self):
        run = self.run_object()
        with patch.object(LiveRun, "call", new_callable=AsyncMock) as provider:
            provider.side_effect = [ModelCallUnknown("unknown"), "ok", ModelCallUnknown("unknown")]
            with self.assertRaises(ModelCallUnknown):
                await run.call("probe", "instructions", [])
            self.assertEqual(await run.call("probe", "instructions", []), "ok")
            with self.assertRaises(ModelCallUnknown):
                await run.call("probe", "instructions", [])
            self.assertEqual(run.consecutive_upstream_errors, 1)
            self.assertFalse(run.report.get("upstream_circuit_open", False))

    async def test_identical_format_protocol_without_reference_for_all_arms(self):
        run = self.run_object()
        case = {"turns": ["来话"], "reference": "隐藏的真人参考"}
        with patch.object(LiveRun, "generate", new_callable=AsyncMock) as generate:
            for arm, text in (("previous", "旧风格"), ("candidate", "新风格")):
                await run.generate("real", case, 0, arm, text)
                sent_prompt = generate.call_args.args[-1]
                self.assertEqual(sent_prompt, text + "\n\n" + GENERATION_FORMAT)
                self.assertNotIn(case["reference"], sent_prompt)

    async def test_plain_generation_keeps_text_and_shared_budget(self):
        from lisa import model_calls as mc

        settings = Settings(
            _env_file=None,
            db_password="unused",
            model_api_key="synthetic",
            model_name="deepseek-v4.1-flash",
            model_reasoning_effort="high",
            model_api_protocol="chat_completions",
        )
        for raw in ("就接这一句。", '{"text":"这是原始输出，不能自动拆出来"}'):
            with (
                self.subTest(raw=raw),
                patch.object(mc, "_reserve", return_value="call"),
                patch.object(mc, "_finish") as finish,
                patch.object(mc, "_provider_request", new_callable=AsyncMock) as provider,
            ):
                provider.return_value = SimpleNamespace(
                    status="completed",
                    output_text=raw,
                    usage=SimpleNamespace(input_tokens=10, output_tokens=14),
                )
                result = await generate_for_benchmark(
                    settings,
                    object(),
                    "task",
                    "user",
                    "prompt",
                    [],
                    schema=TrialOutput,
                )
                self.assertEqual(result.text, raw)
                self.assertEqual(provider.call_args.args[-1]["text"]["format"], {"type": "text"})
                self.assertEqual(provider.call_args.args[-1]["reasoning"], {"effort": "high"})
                self.assertEqual(provider.call_args.kwargs["endpoint"], "responses")
                self.assertEqual(finish.call_args.kwargs["state"], "succeeded")

    async def test_reuse_only_previous_output_and_keep_source_immutable(self):
        run = self.run_object()
        run.report["generations"] = {}
        run.reused_baseline = {
            "run_id": "frozen",
            "generations": {"real/Q1/r0/previous": {"turns": ["旧版原始输出"]}},
        }
        with patch.object(LiveRun, "generate", new_callable=AsyncMock) as generate:
            value = await run.generate("real", {"id": "Q1"}, 0, "previous", "old")
            generate.assert_not_awaited()
            self.assertEqual(value["turns"], ["旧版原始输出"])
            self.assertEqual(value["reused_from_run"], "frozen")
            self.assertNotIn(
                "reused_from_run", run.reused_baseline["generations"]["real/Q1/r0/previous"]
            )
            await run.generate("real", {"id": "Q1"}, 0, "candidate", "new")
            generate.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
