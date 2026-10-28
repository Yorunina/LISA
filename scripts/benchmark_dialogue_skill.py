"""Frozen paired skill content evaluation; no QQ, worker, or production configuration changes."""

import argparse
import asyncio
import importlib.util
import json
import random
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.dialogue_skill import (  # noqa: E402
    PRIVATE_SKILL_ROOT,
    SKILL_ROOT,
    compile_dialogue_skill,
    review_guide,
)
from lisa.dialogue_skill_review import generate_reviewed_reply  # noqa: E402
from lisa.models import Account  # noqa: E402
from scripts.benchmark_chat_distillation import (  # noqa: E402
    Judgments,
    calibration_case,
    checked_judgments,
)
from scripts.benchmark_chat_prompt import group_summary, scored_rows, sha  # noqa: E402
from scripts.benchmark_real_chat_prompt import (  # noqa: E402
    GENERATION_FORMAT,
    PREVIOUS,
    RealRun,
    cluster_summary,
)
from scripts.benchmark_real_chat_prompt import (  # noqa: E402
    generate_for_benchmark as plain_generate,
)

DATA = ROOT / "runtime/reports/chat-skill-gold-v1"
JUDGE = ROOT / ".docs/benchmarks/prompts/chat_skill_judge_v1.txt"
PLAN = ROOT / ".docs/benchmarks/CHAT_SKILL_PLAN.md"
IDENTITY = "你是LISA，正在和熟人用中文交流。\n"


def validate_data(dev, confirm, guards):
    assert len(confirm["cases"]) == 28
    assert len({c["episode"] for c in confirm["cases"]}) >= 18
    used = {
        i
        for c in dev["cases"]
        for i in range(c["source"]["incoming_ordinal"] - 5, c["source"]["reply_ordinal"] + 6)
    }
    old_hold = json.loads(
        (ROOT / "runtime/reports/real-chat-gold-v1/confirm.json").read_text(encoding="utf-8")
    )
    used.update(
        i
        for c in old_hold["cases"]
        for i in range(c["source"]["incoming_ordinal"] - 5, c["source"]["reply_ordinal"] + 6)
    )
    for c in confirm["cases"]:
        assert not used.intersection(c["input_ordinals"] + [c["source"]["reply_ordinal"]])
        assert max(c["input_ordinals"]) < c["source"]["reply_ordinal"]
        assert c["reference"] and c["expected"]
    assert len(guards["cases"]) == 8
    return {
        "real_cases": 28,
        "episodes": len({c["episode"] for c in confirm["cases"]}),
        "guards": 8,
        "old_source_overlap": 0,
    }


def summarize_skill(report):
    rows = scored_rows(report)
    result = {
        "groups": {},
        "calls": len(report["calls"]),
        "estimated_cny": str(sum((Decimal(c["cost"]) for c in report["calls"]), Decimal(0))),
    }
    for group in sorted({p["group"] for p in report["pairs"]}):
        subset = [r for r in rows if r["group"] == group]
        result["groups"][group] = {
            "cases": group_summary(subset),
            "episodes": cluster_summary(subset, report["pairs"]),
        }
    if report["stage"] == "confirm":
        mode = report["modes"][0]
        primary = [r for r in rows if r["group"] == mode + "_real"]
        reverse = [r for r in scored_rows(report, "reversed") if r["group"] == mode + "_real"]
        lookup = {r["id"]: r["winner"] for r in primary}
        agreement = sum(lookup[r["id"]] == r["winner"] for r in reverse)
        stats = group_summary(primary)
        episodes = cluster_summary(primary, report["pairs"])
        guard = result["groups"].get(mode + "_guard", {}).get("cases", {})
        gates = {
            "coverage": len(primary) >= 26 and episodes["independent_cases"] >= 18,
            "high_absolute_score": (stats["mean_naturalness"]["candidate"] or 0) >= 3.5,
            "paired_effect": (episodes["mean_delta"] or 0) >= 0.3,
            "positive_cluster_interval": bool(
                episodes["cluster_bootstrap_95"] and episodes["cluster_bootstrap_95"][0] > 0
            ),
            "cluster_sign_test": episodes["case_sign_test_two_sided_p"] < 0.05,
            "position_stability": len(reverse) >= 26 and agreement / len(reverse) >= 0.75,
            "guards": guard.get("pairs_scored") == 8
            and guard["critical_passes"]["candidate"] == 8
            and guard["fabrications"]["candidate"] == 0,
            "critical_nonregression": stats["critical_passes"]["candidate"]
            >= stats["critical_passes"]["baseline"]
            and stats["fabrications"]["candidate"] <= stats["fabrications"]["baseline"],
            "calibration": report.get("calibration", {}).get("passed", False),
        }
        result.update(
            gates=gates,
            criteria_met=all(gates.values()),
            position_checks={"graded": len(reverse), "agrees": agreement},
        )
    return result


class SkillRun(RealRun):
    def __init__(self, args, dev, confirm, guards):
        super().__init__(args, dev, confirm)
        self.guards = guards
        files = [
            Path(__file__).resolve(),
            ROOT / "lisa/dialogue_skill.py",
            ROOT / "lisa/dialogue_skill_review.py",
            PLAN,
            JUDGE,
            *SKILL_ROOT.rglob("*.md"),
            *DATA.glob("*.json"),
            DATA / "build_gold.py",
        ]
        if {"corpus", "review"}.intersection(args.modes):
            files.extend(PRIVATE_SKILL_ROOT.rglob("*.md"))
            files.extend(PRIVATE_SKILL_ROOT.rglob("*.json"))
        for path in files:
            name = str(path.relative_to(ROOT))
            self.hashes[name] = sha(path.read_bytes())
            target = self.folder / "inputs" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        self.report.update(
            experiment="natural_dialogue_skill_v1",
            modes=args.modes,
            generation_output_tokens=self.settings.model_max_output_tokens,
            judge_output_tokens=getattr(args, "judge_output_tokens", 6000),
            source_sha256=self.hashes,
            generations={},
            pairs=[],
            judgments=[],
            calls=[],
            drafts={},
        )
        self.review_text = review_guide() if "review" in args.modes else ""
        if self.review_text:
            self.report["review_guide_sha256"] = sha(self.review_text.encode())
            self.report["candidate_provider_calls_per_turn"] = 2
        self.freeze_model_calls()

    def freeze_model_calls(self, snapshot_path=None):
        """Execute the recorded transport snapshot while other chats edit the checkout."""
        path = snapshot_path or self.folder / "inputs/lisa/model_calls.py"
        digest = sha(path.read_bytes())
        name = "lisa._skill_transport_" + digest[:16]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        self._frozen_model_calls = module
        # Keep the original provenance manifest; validation follows the file actually executed.
        self.report["source_sha256"] = dict(self.report["source_sha256"])
        for key in list(self.hashes):
            if Path(key) == Path("lisa/model_calls.py"):
                del self.hashes[key]
        self.hashes[str(path.relative_to(ROOT))] = digest
        self.report["execution_model_calls"] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": digest,
        }

    async def call(self, *args, **kwargs):
        # Reasoning and structured grading share this allowance on the relay.
        # Apply it equally to both arms; generation retains its original cap.
        if args and args[0].startswith("judge/"):
            args = list(args)
            if len(args) >= 5:
                args[4] = self.report.get("judge_output_tokens", 6000)
            else:
                kwargs["tokens"] = self.report.get("judge_output_tokens", 6000)
        if any(sha((ROOT / name).read_bytes()) != digest for name, digest in self.hashes.items()):
            self.report["source_drift"] = True
            self.save()
            raise RuntimeError("source_drift")
        frozen = self._frozen_model_calls
        label = args[0] if args else ""
        reviewing = "review" in self.args.modes and (
            "/r0/review/" in label or label.startswith("integration/")
        )

        async def reviewed(settings, engine, task_id, user_id, instructions, messages, **options):
            from lisa.style_distillation import TrialOutput

            async def stage(cfg, eng, tid, uid, text, history, **kwargs):
                if kwargs["schema"] is TrialOutput:
                    text += "\n\n" + GENERATION_FORMAT
                return await plain_generate(cfg, eng, tid, uid, text, history, **kwargs)

            if options["schema"] is TrialOutput:
                instructions = instructions.removesuffix("\n\n" + GENERATION_FORMAT)

            def record(draft):
                self.report["drafts"][label] = draft
                self.save()

            return await generate_reviewed_reply(
                settings,
                engine,
                task_id,
                user_id,
                instructions,
                messages,
                review_text=self.review_text,
                generate=stage,
                on_draft=record,
                **options,
            )

        with (
            patch("scripts.benchmark_real_chat_prompt.mc", frozen),
            patch("scripts.benchmark_chat_prompt._reply_request", frozen._reply_request),
            patch("scripts.benchmark_chat_prompt._cost", frozen._cost),
            patch("scripts.benchmark_chat_prompt.request_size", frozen.request_size),
            patch(
                "scripts.benchmark_real_chat_prompt.generate_for_benchmark",
                reviewed if reviewing else plain_generate,
            ),
        ):
            for attempt in range(2):
                try:
                    return await super().call(*args, **kwargs)
                except Exception as exc:
                    last = self.report["calls"][-1] if self.report["calls"] else {}
                    retry = (
                        attempt == 0
                        and args
                        and args[0].startswith("judge/")
                        and type(exc).__name__ == "ModelCallFailed"
                        and last.get("state") == "failed"
                        and last.get("error") == "response_not_completed"
                    )
                    if not retry:
                        raise
                    self.report.setdefault("grading_transport_retries", []).append(
                        {
                            "label": args[0],
                            "reason": "settled_incomplete",
                            "attempt": 1,
                        }
                    )
                    self.save()

    async def grade(self, rows, role):
        with patch("scripts.benchmark_real_chat_prompt.JUDGE", JUDGE):
            for row in rows:
                await super().grade([row], role)

    async def calibrate(self):
        cases = [
            calibration_case(self.gold["calibration"]),
            {
                "case_id": "negative_fact_calibration",
                "context": ["你那边也断网了吗？"],
                "reference": "你说的是哪一片？",
                "expected": "没有自身所在地或网络状态，肯定和否定都不能编造。",
                "a": ["你说的是哪一片？"],
                "b": ["我这边没断网。"],
            },
        ]
        value = await self.call(
            "judge/calibration",
            JUDGE.read_text(encoding="utf-8"),
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"cases": cases, "rubric": self.gold["rubric"]}, ensure_ascii=False
                    ),
                }
            ],
            Judgments,
            2200,
        )
        scores = checked_judgments(value, [c["case_id"] for c in cases])
        by_id = {s["case_id"]: s for s in scores}
        style = by_id[cases[0]["case_id"]]
        fact = by_id["negative_fact_calibration"]
        passed = (
            style["winner"] == "a"
            and style["b"]["counseling"]
            and fact["b"]["fabrication"]
            and not fact["a"]["fabrication"]
        )
        self.report["calibration"] = {"passed": passed, "scores": scores}
        self.save()
        if not passed:
            raise RuntimeError("judge_calibration_failed")

    async def execute(self):
        prompts = {
            "previous": PREVIOUS.read_text(encoding="utf-8"),
            **{mode: IDENTITY + compile_dialogue_skill(mode) for mode in self.args.modes},
        }
        self.report.update(
            prompt_hashes={k: sha(v.encode()) for k, v in prompts.items()},
            wire_prompt_hashes={
                k: sha((v + "\n\n" + GENERATION_FORMAT).encode()) for k, v in prompts.items()
            },
            generation_transport="responses_plain_text_v1",
            shared_generation_output_protocol=GENERATION_FORMAT,
        )
        for name, prompt in prompts.items():
            (self.folder / (name + "_prompt.txt")).write_text(prompt, encoding="utf-8")
        with Session(self.engine) as db, db.begin():
            user = Account(username="skillbench_" + self.folder.name[-6:], password_hash="!")
            db.add(user)
            db.flush()
            self.uid = user.id
        self.save()
        try:
            await self.calibrate()
            real = (self.gold if self.args.stage == "develop" else self.hold)["cases"]
            for kind, cases in (("real", real), ("guard", self.guards["cases"])):
                for case in random.Random("skill-" + kind).sample(cases, len(cases)):
                    outputs = {}
                    arms = random.Random("arms-" + case["id"]).sample(list(prompts), len(prompts))
                    for arm in arms:
                        outputs[arm] = await self.generate(kind, case, 0, arm, prompts[arm])
                        if self.report.get("upstream_circuit_open"):
                            raise RuntimeError("upstream_circuit_open")
                        if self.report.get("source_drift"):
                            raise RuntimeError("source_drift")
                    for mode in self.args.modes:
                        group = mode + "_" + kind
                        self.report["pairs"].append(
                            {
                                "id": group + "/" + case["id"],
                                "group": group,
                                "case": case,
                                "rep": 0,
                                "variant": "candidate",
                                "outputs": {
                                    "baseline": outputs["previous"],
                                    "candidate": outputs[mode],
                                },
                            }
                        )
                    if {"core", "corpus"}.issubset(self.args.modes):
                        group = "corpus_vs_core_" + kind
                        self.report["pairs"].append(
                            {
                                "id": group + "/" + case["id"],
                                "group": group,
                                "case": case,
                                "rep": 0,
                                "variant": "candidate",
                                "outputs": {
                                    "baseline": outputs["core"],
                                    "candidate": outputs["corpus"],
                                },
                            }
                        )
                    self.save()
            valid = [
                p
                for p in self.report["pairs"]
                if all(not output.get("error") for output in p["outputs"].values())
            ]
            random.Random(20261028).shuffle(valid)
            for start in range(0, len(valid), 2):
                await self.grade(valid[start : start + 2], "primary")
            if self.args.stage == "confirm":
                for start in range(0, len(valid), 2):
                    await self.grade(valid[start : start + 2], "reversed")
            self.report["status"] = (
                "completed"
                if len(scored_rows(self.report)) == len(self.report["pairs"])
                else "partial"
            )
        except Exception as exc:
            self.report.update(
                status="partial", error=type(exc).__name__, stop_reason=str(exc)[:150]
            )
        finally:
            with Session(self.engine) as db, db.begin():
                db.execute(delete(Account).where(Account.id == self.uid))
            self.engine.dispose()
            self.report.update(
                temporary_account_removed=True, finished_at_utc=datetime.now(UTC).isoformat()
            )
            self.report["summary"] = summarize_skill(self.report)
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
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("core", "examples", "corpus", "review"),
        default=["core", "examples"],
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-cost", type=Decimal, default=Decimal("10"))
    parser.add_argument("--max-calls", type=int, default=180)
    args = parser.parse_args()
    if args.stage == "confirm" and len(args.modes) != 1:
        parser.error("Freeze exactly one candidate before confirmation")
    args.prompt = SKILL_ROOT / "SKILL.md"
    args.model = None
    args.reuse_baseline = None
    dev, confirm, guards = [
        json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("develop", "confirm", "guards")
    ]
    check = validate_data(dev, confirm, guards)
    for mode in args.modes:
        prompt = compile_dialogue_skill(mode)
        assert all(
            c["reference"] not in prompt for c in confirm["cases"] if len(c["reference"]) >= 8
        )
    print(json.dumps({**check, "stage": args.stage, "live": args.live}), flush=True)
    if args.live:
        asyncio.run(SkillRun(args, dev, confirm, guards).execute())


if __name__ == "__main__":
    main()
