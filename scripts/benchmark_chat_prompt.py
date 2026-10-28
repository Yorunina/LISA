"""Bounded direct-prompt development and frozen confirmation; never run distillation."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.chat_style import BASELINE, expression_guide  # noqa: E402
from lisa.db import make_engine  # noqa: E402
from lisa.model_calls import _cost, _reply_request, generate_structured, request_size  # noqa: E402
from lisa.models import Account, ModelCall, Task  # noqa: E402
from lisa.settings import Settings  # noqa: E402
from lisa.style_distillation import TrialOutput  # noqa: E402
from scripts.benchmark_chat_distillation import (  # noqa: E402
    Judgments,
    calibration_case,
    checked_judgments,
)

GOLD = ROOT / "tests/fixtures/chat_distillation_gold_v1.json"
HOLD = ROOT / "tests/fixtures/chat_direct_confirmation_v1.json"
PROMPTS = ROOT / ".docs/benchmarks/prompts"
JUDGE = PROMPTS / "chat_direct_judge_v1.txt"
DEVELOPMENT_IDS = {"D01", "D03", "D04", "W01", "W03", "W05"}
GOLD_CRITICAL = {"D04", "D05", "D07", "W04", "W07"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def messages_for(case, prior, text):
    """Only actual dialogue reaches generation; reference and rubric stay with judges."""
    return [*case.get("history", []), *prior, {"role": "user", "content": text}]


def ordered_check(case, texts):
    if not case.get("ordered_terms"):
        return None
    text = "\n".join(texts)
    positions = [text.find(term) for term in case["ordered_terms"]]
    return min(positions) >= 0 and positions == sorted(positions)


def pair_data(row, order):
    case = row["case"]
    mapping = dict(zip(("a", "b"), order))
    content = {
        "case_id": row["id"],
        "context": [*case.get("history", []), *case["turns"]],
        "reference": case["reference"],
        "expected": case["expected"],
        "persona": "以参考答复的自然口吻和接话分寸为准，不移植参考中的身份或经历。",
        **{key: row["outputs"][name]["turns"] for key, name in mapping.items()},
    }
    return content, mapping


def scored_rows(report, role="primary"):
    rows = []
    lookup = {row["id"]: row for row in report["pairs"]}
    for batch in report["judgments"]:
        if batch["role"] != role or batch.get("error"):
            continue
        for score in batch["scores"]:
            row = lookup.get(score["case_id"])
            if row is None:
                continue
            mapping = batch["mapping"][score["case_id"]]
            ratings = {mapping[k]: score[k] for k in ("a", "b")}
            variant = row["variant"]
            rows.append(
                {
                    "id": row["id"],
                    "case_id": row["case"]["id"],
                    "group": row["group"],
                    "rep": row["rep"],
                    "variant": variant,
                    "winner": mapping.get(score["winner"], score["winner"]),
                    "baseline": ratings["baseline"],
                    "candidate": ratings[variant],
                    "critical": row["case"].get("critical", False),
                    "auto_checks": {
                        v: row["outputs"][v].get("ordered_check") for v in ("baseline", variant)
                    },
                }
            )
    return rows


def group_summary(rows):
    by_case = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    means = {
        name: [statistics.mean(r[name]["naturalness"] for r in group) for group in by_case.values()]
        for name in ("baseline", "candidate")
    }
    deltas = {
        key: statistics.mean(
            r["candidate"]["naturalness"] - r["baseline"]["naturalness"] for r in group
        )
        for key, group in by_case.items()
    }
    values = list(deltas.values())
    npos, nneg = sum(v > 0 for v in values), sum(v < 0 for v in values)
    n = npos + nneg
    rng = random.Random(20261027)
    boot = (
        sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(4000))
        if values
        else []
    )
    critical = [r for r in rows if r["critical"]]

    def compliant(row, arm):
        key = "baseline" if arm == "baseline" else row["variant"]
        return row[arm]["rubric_met"] and row["auto_checks"].get(key) is not False

    return {
        "pairs_scored": len(rows),
        "independent_cases": len(by_case),
        "mean_naturalness": {
            k: round(statistics.mean(v), 4) if v else None for k, v in means.items()
        },
        "case_mean_deltas": deltas,
        "mean_delta": round(statistics.mean(values), 4) if values else None,
        "cluster_bootstrap_95": [round(boot[100], 4), round(boot[3899], 4)] if boot else None,
        "case_signs": {"positive": npos, "negative": nneg, "zero": len(values) - n},
        "case_sign_test_two_sided_p": min(
            1.0, 2 * sum(math.comb(n, k) for k in range(min(npos, nneg) + 1)) / 2**n
        )
        if n
        else 1.0,
        "preference_counts": {
            k: sum(r["winner"] == k for r in rows)
            for k in ({r["variant"] for r in rows} | {"baseline", "tie", "uncertain"})
        },
        "critical_pairs": len(critical),
        "critical_cases": sorted({r["case_id"] for r in critical}),
        "critical_passes": {
            name: sum(compliant(r, name) for r in critical) for name in ("baseline", "candidate")
        },
        "fabrications": {
            name: sum(r[name]["fabrication"] for r in rows) for name in ("baseline", "candidate")
        },
    }


def summarize(report):
    rows = scored_rows(report)
    result = {
        "groups": {},
        "call_count": len(report["calls"]),
        "estimated_cny": str(sum((Decimal(c["cost"]) for c in report["calls"]), Decimal(0))),
    }
    for group in sorted({p["group"] for p in report["pairs"]}):
        for variant in sorted({p["variant"] for p in report["pairs"] if p["group"] == group}):
            relevant = [r for r in rows if r["group"] == group and r["variant"] == variant]
            stats = group_summary(relevant)
            stats["pairs_scheduled"] = sum(
                p["group"] == group and p["variant"] == variant for p in report["pairs"]
            )
            result["groups"][group + ":" + variant] = stats
    if report["stage"] == "develop":
        eligible = [
            (
                value["mean_delta"],
                -value["case_signs"]["negative"],
                value["mean_naturalness"]["candidate"],
                key,
            )
            for key, value in result["groups"].items()
            if value["pairs_scored"] >= 5
            and value["critical_passes"]["candidate"] >= value["critical_passes"]["baseline"]
            and value["fabrications"]["candidate"] <= value["fabrications"]["baseline"]
        ]
        result["selected_variant"] = max(eligible)[-1].split(":", 1)[1] if eligible else None
    else:
        primary = {row["id"]: row for row in rows}
        reverse = scored_rows(report, "reversed")
        checks = [
            {"id": row["id"], "agrees": row["winner"] == primary[row["id"]]["winner"]}
            for row in reverse
            if row["id"] in primary
        ]
        result["position_checks"] = checks
        fresh = result["groups"].get("confirmation:candidate", {})
        cp = fresh.get("critical_passes", {})
        cn = fresh.get("critical_pairs", 0)
        gates = {
            "coverage": fresh.get("pairs_scored", 0) >= 22
            and fresh.get("independent_cases", 0) >= 11,
            "effect_size": (fresh.get("mean_delta") or 0) >= 0.5,
            "positive_cluster_interval": bool(
                fresh.get("cluster_bootstrap_95") and fresh["cluster_bootstrap_95"][0] > 0
            ),
            "case_significance": fresh.get("case_sign_test_two_sided_p", 1) < 0.05,
            "position_stability": len(checks) >= 10
            and sum(c["agrees"] for c in checks) / len(checks) >= 0.75,
            "critical_behavior": cn >= 10
            and len(fresh.get("critical_cases", [])) == 6
            and cp.get("candidate", 0) / cn >= 0.9
            and cp.get("candidate", 0) >= cp.get("baseline", 0)
            if cn
            else False,
            "calibration": report.get("calibration", {}).get("passed", False),
        }
        result["confirmation_gates"] = gates
        result["criteria_met"] = all(gates.values())
    return result


class LiveRun:
    def __init__(self, args, gold, hold):
        self.args, self.gold, self.hold = args, gold, hold
        self.settings = Settings()
        self.folder = (
            ROOT
            / "runtime/reports/direct-chat-prompt"
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
            GOLD,
            HOLD,
            JUDGE,
            Path(__file__).resolve(),
            ROOT / "lisa/model_calls.py",
            ROOT / "lisa/structured_output.py",
            ROOT / "lisa/settings.py",
            ROOT / "lisa/chat_style.py",
            ROOT / "lisa/style_distillation.py",
            ROOT / "lisa/prompts/chat_expression.txt",
            ROOT / "scripts/benchmark_chat_distillation.py",
            ROOT / ".docs/benchmarks/CHAT_DIRECT_PROMPT_PLAN.md",
            PROMPTS / "chat_direct_a_v1.txt",
            PROMPTS / "chat_direct_b_v1.txt",
        ]
        if args.prompt:
            files.append(args.prompt)
        self.hashes = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in set(files)}
        for path in files:
            target = self.folder / "inputs" / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        self.report = {
            "run_id": self.folder.name,
            "stage": args.stage,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "status": "running",
            "source_sha256": self.hashes,
            "model": self.settings.model_name,
            "reasoning": self.settings.model_reasoning_effort,
            "sdk": importlib.metadata.version("openai"),
            "base_url": self.settings.model_base_url,
            "max_calls": args.max_calls,
            "max_estimated_cny": str(args.max_cost),
            "no_distillation": True,
            "reference_priority": True,
            "generations": {},
            "pairs": [],
            "judgments": [],
            "calls": [],
        }
        self.engine = make_engine(self.settings)
        self.uid = None

    def save(self):
        (self.folder / "results.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def call(self, label, instructions, messages, schema=TrialOutput, tokens=None):
        settings = self.settings
        tokens = tokens or settings.model_max_output_tokens
        assert all(
            sha((ROOT / name).read_bytes()) == value for name, value in self.hashes.items()
        ), "source_drift"
        spent = sum((Decimal(c["cost"]) for c in self.report["calls"]), Decimal(0))
        request = _reply_request(
            settings.model_copy(update={"model_max_output_tokens": tokens}),
            instructions,
            messages,
            schema,
        )
        amount = _cost(settings, request_size(request) + 512, tokens)
        if len(self.report["calls"]) >= self.args.max_calls or spent + amount > self.args.max_cost:
            raise RuntimeError("experiment_budget_cap")
        with Session(self.engine) as db, db.begin():
            task = Task(
                user_id=self.uid,
                kind="model_probe",
                source="development",
                status="running",
                started_at=datetime.now(UTC),
                request={"prompt_experiment": label},
            )
            db.add(task)
            db.flush()
            tid = task.id
        started, failure = time.monotonic(), None
        try:
            return await generate_structured(
                settings,
                self.engine,
                tid,
                self.uid,
                instructions,
                messages,
                schema=schema,
                output_tokens=tokens,
            )
        except Exception as exc:
            failure = type(exc).__name__
            raise
        finally:
            with Session(self.engine) as db, db.begin():
                task = db.get(Task, tid)
                task.status = "failed" if failure else "completed"
                task.error, task.finished_at = failure, datetime.now(UTC)
                for c in db.scalars(select(ModelCall).where(ModelCall.task_id == tid)):
                    self.report["calls"].append(
                        {
                            "label": label,
                            "state": c.state,
                            "error": c.error,
                            "input_tokens": c.input_tokens,
                            "output_tokens": c.output_tokens,
                            "seconds": round(time.monotonic() - started, 2),
                            "cost": str(
                                c.charged_usd if c.charged_usd is not None else c.reserved_usd
                            ),
                        }
                    )
            self.save()
            print(
                json.dumps(
                    {"stage": label, "calls": len(self.report["calls"]), "ok": failure is None}
                ),
                flush=True,
            )

    async def generate(self, group, case, rep, variant, prompt):
        key = f"{group}/{case['id']}/r{rep}/{variant}"
        prior, output = [], {"turns": []}
        try:
            for i, text in enumerate(case["turns"]):
                value = await self.call(key + f"/t{i}", prompt, messages_for(case, prior, text))
                output["turns"].append(value.text)
                prior.extend(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": value.text},
                    ]
                )
            output["ordered_check"] = ordered_check(case, output["turns"])
        except Exception as exc:
            output["error"] = type(exc).__name__
        self.report["generations"][key] = output
        self.save()
        return output

    async def grade(self, rows, role):
        items, mappings = [], {}
        for row in rows:
            order = random.Random("grade-" + row["id"]).sample(["baseline", row["variant"]], 2)
            if role == "reversed":
                order.reverse()
            item, mapping = pair_data(row, order)
            items.append(item)
            mappings[row["id"]] = mapping
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
        base = BASELINE + "\n" + expression_guide() + "\n当前交流指引：\n" + BASELINE
        if self.args.stage == "develop":
            variants = {
                "candidate_a": (PROMPTS / "chat_direct_a_v1.txt").read_text(encoding="utf-8"),
                "candidate_b": (PROMPTS / "chat_direct_b_v1.txt").read_text(encoding="utf-8"),
            }
            if self.args.prompt:
                variants = {"candidate_c": self.args.prompt.read_text(encoding="utf-8")}
            groups = [
                ("development", [c for c in self.gold["cases"] if c["id"] in DEVELOPMENT_IDS], 1)
            ]
        else:
            variants = {"candidate": self.args.prompt.read_text(encoding="utf-8")}
            groups = [
                ("gold_regression", self.gold["cases"], 1),
                ("confirmation", self.hold["cases"], 2),
            ]
        self.report["prompt_hashes"] = {
            "baseline": sha(base.encode()),
            **{k: sha(v.encode()) for k, v in variants.items()},
        }
        with Session(self.engine) as db, db.begin():
            user = Account(username="promptbench_" + uuid4().hex[:18], password_hash="!")
            db.add(user)
            db.flush()
            self.uid = user.id
        try:
            for group, cases, reps in groups:
                cases = [
                    dict(c, critical=c.get("critical", c["id"] in GOLD_CRITICAL)) for c in cases
                ]
                random.Random(20261027 + len(group)).shuffle(cases)
                for rep in range(reps):
                    for case in cases:
                        outputs = {}
                        names = list(variants) + ["baseline"]
                        random.Random(f"{group}-{case['id']}-{rep}").shuffle(names)
                        for name in names:
                            outputs[name] = await self.generate(
                                group,
                                case,
                                rep,
                                name,
                                base if name == "baseline" else variants[name],
                            )
                        for variant in variants:
                            self.report["pairs"].append(
                                {
                                    "id": f"{group}/{case['id']}/r{rep}/{variant}",
                                    "group": group,
                                    "case": case,
                                    "rep": rep,
                                    "variant": variant,
                                    "outputs": {
                                        "baseline": outputs["baseline"],
                                        variant: outputs[variant],
                                    },
                                }
                            )
                        self.save()
            cal = self.gold["calibration"]
            try:
                value = await self.call(
                    "judge/calibration",
                    JUDGE.read_text(encoding="utf-8"),
                    [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"rubric": self.gold["rubric"], "cases": [calibration_case(cal)]},
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
                if all(not v.get("error") for v in r["outputs"].values())
            ]
            random.Random(20261027).shuffle(valid)
            for offset in range(0, len(valid), 2):
                await self.grade(valid[offset : offset + 2], "primary")
            if self.args.stage == "confirm":
                reverse = [r for r in valid if r["group"] == "confirmation" and r["rep"] == 0]
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
            if self.uid is not None:
                with Session(self.engine) as db, db.begin():
                    db.execute(delete(Account).where(Account.id == self.uid))
                self.report["temporary_account_removed"] = True
            self.engine.dispose()
            self.report["finished_at_utc"] = datetime.now(UTC).isoformat()
            self.report["summary"] = summarize(self.report)
            self.save()
            (self.folder / "summary.json").write_text(
                json.dumps(self.report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "folder": str(self.folder),
                        "status": self.report["status"],
                        "summary": self.report["summary"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["develop", "confirm"], required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--max-cost", type=Decimal)
    parser.add_argument("--max-calls", type=int, default=120)
    args = parser.parse_args()
    if args.stage == "confirm" and not args.prompt:
        parser.error("--prompt is required after selecting and freezing a candidate")
    if args.prompt:
        args.prompt = args.prompt.resolve()
        args.prompt.relative_to(ROOT)
    args.max_cost = args.max_cost or Decimal("0.80" if args.stage == "develop" else "2.20")
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    hold = json.loads(HOLD.read_text(encoding="utf-8"))
    assert {c["id"] for c in gold["cases"]} >= DEVELOPMENT_IDS
    assert len(hold["cases"]) == len({c["id"] for c in hold["cases"]}) == 12
    assert sum(c["critical"] for c in hold["cases"]) == 6
    for path in [
        PROMPTS / "chat_direct_a_v1.txt",
        PROMPTS / "chat_direct_b_v1.txt",
        *([args.prompt] if args.prompt else []),
    ]:
        text = path.read_text(encoding="utf-8")
        assert all(c["reference"] not in text for c in [*gold["cases"], *hold["cases"]])
    print(
        json.dumps(
            {
                "stage": args.stage,
                "live": args.live,
                "development_cases": 6,
                "confirmation_cases": 12,
                "max_cost": str(args.max_cost),
            }
        ),
        flush=True,
    )
    if args.live:
        asyncio.run(LiveRun(args, gold, hold).execute())


if __name__ == "__main__":
    main()
