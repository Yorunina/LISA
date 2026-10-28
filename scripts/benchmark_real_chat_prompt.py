"""Private, bounded real-source conversation prompt development and confirmation."""

import argparse
import asyncio
import copy
import importlib.metadata
import json
import random
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa import model_calls as mc  # noqa: E402
from lisa.chat_style import BASELINE, expression_guide  # noqa: E402
from lisa.db import make_engine  # noqa: E402
from lisa.models import Account  # noqa: E402
from lisa.settings import Settings  # noqa: E402
from lisa.style_distillation import TrialOutput  # noqa: E402
from scripts.benchmark_chat_distillation import (  # noqa: E402
    Judgments,
    calibration_case,
    checked_judgments,
)
from scripts.benchmark_chat_prompt import (  # noqa: E402
    LiveRun,
    group_summary,
    scored_rows,
    sha,
)

DATA = ROOT / "runtime/reports/real-chat-gold-v1"
PROMPTS = ROOT / ".docs/benchmarks/prompts"
PREVIOUS = PROMPTS / "chat_direct_selected_v1.txt"
JUDGE = PROMPTS / "chat_real_judge_v1.txt"
PLAN = ROOT / ".docs/benchmarks/CHAT_REAL_PROMPT_PLAN.md"
GUARDS = {"H04", "H07", "H09", "H10"}
GENERATION_FORMAT = (
    "输出协议：只输出本次发给对方的自然语言正文，不包成JSON，不使用JSON代码块，不解释输出格式。"
)


async def generate_for_benchmark(
    settings, engine, task_id, user_id, instructions, messages, *, schema, output_tokens=None
):
    """Plain generation, structured judging; no parser fallback or output rewriting."""
    if schema is not TrialOutput:
        return await mc.generate_structured(
            settings,
            engine,
            task_id,
            user_id,
            instructions,
            messages,
            schema=schema,
            output_tokens=output_tokens,
        )
    if output_tokens is not None:
        settings = settings.model_copy(update={"model_max_output_tokens": output_tokens})
    # This benchmark deliberately freezes Responses plain-text transport;
    # production structured JSON transport must not silently change its samples.
    settings = settings.model_copy(update={"model_api_protocol": "responses"})
    request = mc._reply_request(settings, instructions, messages, TrialOutput)
    request["text"] = {"format": {"type": "text"}}
    size = mc.request_size(request)
    if size > settings.model_max_input_bytes:
        raise mc.ModelInputTooLarge("Benchmark input exceeds configured limit")
    amount = mc._cost(settings, size + 512, settings.model_max_output_tokens)
    call_id = mc._reserve(
        settings,
        engine,
        task_id,
        user_id,
        amount,
        configuration={
            "model": settings.model_name,
            "base_url": settings.model_base_url,
            "request_bytes": size,
            "generation_transport": "plain_text_v1",
            "max_output_tokens": settings.model_max_output_tokens,
        },
    )
    response = await mc._provider_request(settings, engine, call_id, request, endpoint="responses")
    usage = getattr(response, "usage", None)
    it, ot = getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
    known = type(it) is int and type(ot) is int and it >= 0 and ot >= 0
    status, value, error = getattr(response, "status", None), None, None
    if status == "completed":
        try:
            value = TrialOutput(text=response.output_text)
        except (ValueError, TypeError, AttributeError):
            error = "invalid_plain_text"
    else:
        error = "response_not_completed"
    unknown = status not in {"completed", "failed", "incomplete", "cancelled"}
    mc._finish(
        engine,
        call_id,
        state="unknown" if unknown else "succeeded" if value else "failed",
        charged=amount if not known or unknown else mc._cost(settings, it, ot),
        input_tokens=it if known else None,
        output_tokens=ot if known else None,
        error=error or (None if known else "usage_unavailable"),
    )
    if unknown:
        raise mc.ModelCallUnknown("Plain text benchmark outcome unknown")
    if value is None:
        raise mc.ModelCallFailed("Plain text benchmark output invalid")
    return value


def validate_data(dev, confirm):
    sources = []
    for split, data, expected_count in (("develop", dev, 12), ("confirm", confirm, 24)):
        assert len(data["cases"]) == len({c["id"] for c in data["cases"]}) == expected_count
        ids = set()
        for case in data["cases"]:
            s = case["source"]
            assert s["incoming_ordinal"] < s["reply_ordinal"]
            assert s["incoming_speaker"] != s["reply_speaker"]
            assert case["reference"] and case["turns"] and case["expected"]
            assert case["reference"] not in case["turns"]
            day = s["reply_time"][:10]
            assert (day <= "2026-10-20") == (split == "develop")
            ids.update((s["incoming_ordinal"], s["reply_ordinal"]))
        sources.append(ids)
    assert not sources[0] & sources[1], "source_message_leakage"
    return {"developer_cases": 12, "confirm_cases": 24, "source_overlap": 0}


def cluster_summary(rows, pairs):
    lookup = {r["id"]: r["case"].get("episode", r["case"]["id"]) for r in pairs}
    return group_summary([{**r, "case_id": lookup[r["id"]]} for r in rows])


def summary(report):
    rows = scored_rows(report)
    result = {
        "groups": {},
        "call_count": len(report["calls"]),
        "estimated_cny": str(sum((Decimal(x["cost"]) for x in report["calls"]), Decimal(0))),
    }
    for group in sorted({x["group"] for x in report["pairs"]}):
        rr = [x for x in rows if x["group"] == group]
        result["groups"][group] = {
            "case_stats": group_summary(rr),
            "time_cluster_stats": cluster_summary(rr, report["pairs"]),
        }
    if report["stage"] == "confirm":
        rr = [x for x in rows if x["group"] == "real_vs_previous"]
        stats = cluster_summary(rr, report["pairs"])
        primary = {x["id"]: x for x in rr}
        reverse = [x for x in scored_rows(report, "reversed") if x["id"] in primary]
        agrees = sum(x["winner"] == primary[x["id"]]["winner"] for x in reverse)
        guard = result["groups"].get("guard_vs_previous", {}).get("case_stats", {})
        raw = group_summary(rr)
        checks = {
            "coverage": len(rr) >= 22 and stats["independent_cases"] >= 16,
            "effect": (stats["mean_delta"] or 0) >= 0.3,
            "positive_cluster_interval": bool(
                stats["cluster_bootstrap_95"] and stats["cluster_bootstrap_95"][0] > 0
            ),
            "cluster_significance": stats["case_sign_test_two_sided_p"] < 0.05,
            "position_stability": len(reverse) >= 20 and agrees / len(reverse) >= 0.75,
            "guards": guard.get("pairs_scored") == 4 and guard["critical_passes"]["candidate"] == 4,
            "real_key_requirements": raw["critical_passes"]["candidate"]
            >= raw["critical_passes"]["baseline"]
            and raw["fabrications"]["candidate"] <= raw["fabrications"]["baseline"],
            "calibration": report.get("calibration", {}).get("passed", False),
        }
        result.update(
            confirmation_gates=checks,
            criteria_met=all(checks.values()),
            position_checks={"graded": len(reverse), "agrees": agrees},
        )
    return result


class RealRun(LiveRun):
    def __init__(self, args, dev, confirm):
        self.args, self.gold, self.hold = args, dev, confirm
        self.settings = Settings()
        if getattr(args, "model", None):
            self.settings = self.settings.model_copy(
                update={"model_name": args.model, "model_reasoning_effort": "high"}
            )
        self.reused_baseline = None
        if getattr(args, "reuse_baseline", None):
            previous = json.loads(args.reuse_baseline.read_text(encoding="utf-8"))
            assert args.stage == "develop" and previous["stage"] == "develop"
            assert previous["status"] == "completed"
            assert previous["model"] == self.settings.model_name
            assert previous["base_url"] == self.settings.model_base_url
            assert previous["reasoning"] == self.settings.model_reasoning_effort
            assert previous["generation_transport"] == "responses_plain_text_v1"
            assert previous["shared_generation_output_protocol"] == GENERATION_FORMAT
            assert previous["source_sha256"][str((DATA / "develop.json").relative_to(ROOT))] == sha(
                (DATA / "develop.json").read_bytes()
            )
            assert previous["wire_prompt_hashes"]["previous"] == sha(
                (PREVIOUS.read_text(encoding="utf-8") + "\n\n" + GENERATION_FORMAT).encode()
            )
            self.reused_baseline = previous
        self.folder = (
            ROOT
            / "runtime/reports/real-chat-prompt"
            / (
                datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + args.stage
                + "-"
                + uuid4().hex[:6]
            )
        )
        self.folder.mkdir(parents=True)
        files = [
            DATA / "develop.json",
            DATA / "confirm.json",
            DATA / "extraction.json",
            PLAN,
            JUDGE,
            PREVIOUS,
            args.prompt,
            Path(__file__).resolve(),
            ROOT / "scripts/prepare_real_chat_gold.py",
            ROOT / "scripts/benchmark_chat_prompt.py",
            ROOT / "scripts/benchmark_chat_distillation.py",
            ROOT / "lisa/chat_style.py",
            ROOT / "lisa/prompts/chat_expression.txt",
            ROOT / "lisa/model_calls.py",
            ROOT / "lisa/structured_output.py",
            ROOT / "lisa/settings.py",
            ROOT / "lisa/style_distillation.py",
            ROOT / "tests/fixtures/chat_direct_confirmation_v1.json",
        ]
        if self.reused_baseline:
            files.append(args.reuse_baseline)
        self.hashes = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in set(files)}
        for name in self.hashes:
            target = self.folder / "inputs" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.report = {
            "run_id": self.folder.name,
            "stage": args.stage,
            "status": "running",
            "started_at_utc": datetime.now(UTC).isoformat(),
            "source_sha256": self.hashes,
            "model": self.settings.model_name,
            "reasoning": self.settings.model_reasoning_effort,
            "sdk": importlib.metadata.version("openai"),
            "base_url": self.settings.model_base_url,
            "max_calls": args.max_calls,
            "max_estimated_cny": str(args.max_cost),
            "no_distillation": True,
            "private_real_source": True,
            "generations": {},
            "pairs": [],
            "judgments": [],
            "calls": [],
        }
        self.engine, self.uid = make_engine(self.settings), None
        self.consecutive_upstream_errors = 0
        if self.reused_baseline:
            self.report["reused_baseline_run"] = self.reused_baseline["run_id"]

    async def generate(self, group, case, rep, variant, prompt):
        if self.reused_baseline and variant == "previous":
            key = f"{group}/{case['id']}/r{rep}/previous"
            result = copy.deepcopy(self.reused_baseline["generations"][key])
            assert not result.get("error")
            result["reused_from_run"] = self.reused_baseline["run_id"]
            self.report["generations"][key] = result
            self.save()
            return result
        return await super().generate(
            group, case, rep, variant, prompt + "\n\n" + GENERATION_FORMAT
        )

    async def call(self, *args, **kwargs):
        if self.report.get("upstream_circuit_open"):
            raise RuntimeError("upstream_circuit_open")
        try:
            with patch("scripts.benchmark_chat_prompt.generate_structured", generate_for_benchmark):
                value = await super().call(*args, **kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            upstream = status is not None and status >= 500
            upstream = upstream or type(exc).__name__ in {
                "APITimeoutError",
                "APIConnectionError",
                "ModelCallUnknown",
            }
            self.consecutive_upstream_errors = (
                self.consecutive_upstream_errors + 1 if upstream else 0
            )
            if self.consecutive_upstream_errors >= 2:
                self.report["upstream_circuit_open"] = True
                self.save()
            raise
        self.consecutive_upstream_errors = 0
        return value

    async def grade(self, rows, role):
        items, mappings = [], {}
        for row in rows:
            order = random.Random("real-grade-" + row["id"]).sample(["baseline", "candidate"], 2)
            if role == "reversed":
                order.reverse()
            mapping = dict(zip(("a", "b"), order))
            mappings[row["id"]] = mapping
            case = row["case"]
            items.append(
                {
                    "case_id": row["id"],
                    "group": row["group"],
                    "context": [*case.get("history", []), *case["turns"]],
                    "reference": case["reference"],
                    "expected": case["expected"],
                    **{k: row["outputs"][arm]["turns"] for k, arm in mapping.items()},
                }
            )
        record = {"role": role, "mapping": mappings}
        try:
            value = await self.call(
                "judge/" + role + "/" + rows[0]["id"],
                JUDGE.read_text(encoding="utf-8"),
                [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"rubric": self.gold["rubric"], "cases": items}, ensure_ascii=False
                        ),
                    }
                ],
                Judgments,
                2200,
            )
            record["scores"] = checked_judgments(value, [r["id"] for r in rows])
        except Exception as exc:
            record["error"] = type(exc).__name__
        self.report["judgments"].append(record)
        self.save()

    async def execute(self):
        prompts = {
            "previous": PREVIOUS.read_text(encoding="utf-8"),
            "candidate": self.args.prompt.read_text(encoding="utf-8"),
            "original": BASELINE + "\n" + expression_guide() + "\n当前交流指引：\n" + BASELINE,
        }
        self.report["prompt_hashes"] = {k: sha(v.encode()) for k, v in prompts.items()}
        self.report["shared_generation_output_protocol"] = GENERATION_FORMAT
        self.report["generation_transport"] = "responses_plain_text_v1"
        self.report["judge_transport"] = "responses_json_schema"
        self.report["wire_prompt_hashes"] = {
            k: sha((v + "\n\n" + GENERATION_FORMAT).encode()) for k, v in prompts.items()
        }
        guard_data = json.loads(
            (ROOT / "tests/fixtures/chat_direct_confirmation_v1.json").read_text(encoding="utf-8")
        )
        guards = [dict(c, critical=True) for c in guard_data["cases"] if c["id"] in GUARDS]
        real = (self.gold if self.args.stage == "develop" else self.hold)["cases"]
        with Session(self.engine) as db, db.begin():
            user = Account(username="promptbench_" + uuid4().hex[:18], password_hash="!")
            db.add(user)
            db.flush()
            self.uid = user.id
        try:
            for kind, cases in (("real", real), ("guard", guards)):
                cases = random.Random("real-prompt-" + kind).sample(cases, len(cases))
                for case in cases:
                    arms = ["previous", "candidate"]
                    if kind == "real" and self.args.stage == "confirm":
                        arms.append("original")
                    random.Random("arms-" + case["id"]).shuffle(arms)
                    outputs = {}
                    for arm in arms:
                        outputs[arm] = await self.generate(kind, case, 0, arm, prompts[arm])
                        if self.report.get("upstream_circuit_open"):
                            raise RuntimeError("upstream_circuit_open")
                    for control in [a for a in arms if a != "candidate"]:
                        group = kind + "_vs_" + control
                        self.report["pairs"].append(
                            {
                                "id": group + "/" + case["id"],
                                "group": group,
                                "case": case,
                                "rep": 0,
                                "variant": "candidate",
                                "outputs": {
                                    "baseline": outputs[control],
                                    "candidate": outputs["candidate"],
                                },
                            }
                        )
                    self.save()
            try:
                cal = self.gold["calibration"]
                value = await self.call(
                    "judge/calibration",
                    JUDGE.read_text(encoding="utf-8"),
                    [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"cases": [calibration_case(cal)], "rubric": self.gold["rubric"]},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    Judgments,
                    1200,
                )
                score = checked_judgments(value, [cal["case_id"]])[0]
                self.report["calibration"] = {
                    "passed": score["winner"] == "a" and score["b"]["counseling"],
                    "score": score,
                }
            except Exception as exc:
                self.report["calibration"] = {"passed": False, "error": type(exc).__name__}
            valid = [
                r
                for r in self.report["pairs"]
                if all(not x.get("error") for x in r["outputs"].values())
            ]
            random.Random(20261028).shuffle(valid)
            for offset in range(0, len(valid), 2):
                await self.grade(valid[offset : offset + 2], "primary")
            if self.args.stage == "confirm":
                reverse = [r for r in valid if r["group"] == "real_vs_previous"]
                for offset in range(0, len(reverse), 2):
                    await self.grade(reverse[offset : offset + 2], "reversed")
            self.report["status"] = (
                "completed"
                if len(scored_rows(self.report)) == len(self.report["pairs"])
                else "partial"
            )
        except Exception as exc:
            self.report["status"], self.report["error"] = "partial", type(exc).__name__
        finally:
            with Session(self.engine) as db, db.begin():
                db.execute(delete(Account).where(Account.id == self.uid))
            self.report["temporary_account_removed"] = True
            self.engine.dispose()
            self.report["finished_at_utc"] = datetime.now(UTC).isoformat()
            self.report["summary"] = summary(self.report)
            self.save()
            (self.folder / "summary.json").write_text(
                json.dumps(self.report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps({"folder": str(self.folder), "summary": self.report["summary"]}),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("develop", "confirm"), required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", help="Override only this evaluation, never production settings")
    parser.add_argument(
        "--reuse-baseline", type=Path, help="Development only: reuse a frozen matching C run"
    )
    parser.add_argument("--max-cost", type=Decimal)
    parser.add_argument("--max-calls", type=int, default=130)
    args = parser.parse_args()
    args.prompt = args.prompt.resolve()
    args.prompt.relative_to(ROOT)
    if args.reuse_baseline:
        args.reuse_baseline = args.reuse_baseline.resolve()
        args.reuse_baseline.relative_to(ROOT / "runtime/reports/real-chat-prompt")
    args.max_cost = args.max_cost or Decimal("0.4" if args.stage == "develop" else "2.2")
    dev, confirm = [
        json.loads((DATA / (name + ".json")).read_text(encoding="utf-8"))
        for name in ("develop", "confirm")
    ]
    check = validate_data(dev, confirm)
    text = args.prompt.read_text(encoding="utf-8")
    assert all(
        c["reference"] not in text
        for d in (dev, confirm)
        for c in d["cases"]
        if len(c["reference"]) >= 8
    ), "reference_leak"
    print(json.dumps({**check, "stage": args.stage, "live": args.live}), flush=True)
    if args.live:
        asyncio.run(RealRun(args, dev, confirm).execute())


if __name__ == "__main__":
    main()
