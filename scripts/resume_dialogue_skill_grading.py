"""Complete interrupted development grading using its exact saved transport implementation."""

import argparse
import asyncio
import copy
import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.orm import Session

from scripts.benchmark_chat_prompt import scored_rows
from scripts.benchmark_dialogue_skill import DATA, ROOT, SkillRun, summarize_skill


async def resume(folder):
    original = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert original["stage"] == "develop" and original["status"] == "partial"
    assert original.get("calibration", {}).get("passed")
    assert all(not g.get("error") for g in original["generations"].values())
    args = SimpleNamespace(
        stage="develop",
        modes=[original.get("draft_mode", original["modes"][0])],
        max_calls=80,
        max_cost=Decimal("5"),
        model=original["model"],
        reuse_baseline=None,
        prompt=ROOT / "lisa/skills/natural-dialogue/SKILL.md",
    )
    datasets = [
        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
        for n in ("develop", "confirm", "guards")
    ]
    run = SkillRun(args, *datasets)
    assert run.settings.model_base_url == original["base_url"]
    assert run.settings.model_reasoning_effort == original["reasoning"]
    assert run.settings.model_max_output_tokens == original["generation_output_tokens"]
    for key in (
        "scripts/benchmark_real_chat_prompt.py",
        ".docs/benchmarks/prompts/chat_skill_judge_v1.txt",
        "lisa/skills/natural-dialogue/SKILL.md",
        "lisa/skills/natural-dialogue/references/examples.md",
    ):
        assert (ROOT / key).read_bytes() == (folder / "inputs" / key).read_bytes()
    run.freeze_model_calls(ROOT / original["execution_model_calls"]["path"])
    repair_sources = dict(run.hashes)
    execution = copy.deepcopy(run.report["execution_model_calls"])
    (folder / "before_repair_results.json").write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run.folder, run.report = folder, copy.deepcopy(original)
    run.report["repair"] = {
        "execution_source_hashes": repair_sources,
        "execution_model_calls": execution,
        "original_source_drift": original.get("source_drift", False),
        "reason": "complete_missing_grading_without_regenerating",
        "generations_reused_without_changes": True,
    }
    if "isolated_evaluation" in run.report:
        run.report.setdefault("isolated_evaluation_history", []).append(
            copy.deepcopy(run.report.pop("isolated_evaluation"))
        )
    run.report.pop("source_drift", None)
    run.report["status"] = "running"
    from lisa.models import Account

    with Session(run.engine) as db, db.begin():
        user = Account(username="skillrepair_" + folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
    try:
        done = {r["id"] for r in scored_rows(run.report)}
        rows = list(run.report["pairs"])
        random.Random(20261028).shuffle(rows)
        pending = [r for r in rows if r["id"] not in done]
        for start in range(0, len(pending), 2):
            await run.grade(pending[start : start + 2], "primary")
            if run.report.get("source_drift") or run.report.get("upstream_circuit_open"):
                break
        run.report["status"] = (
            "completed" if len(scored_rows(run.report)) == len(rows) else "partial"
        )
    finally:
        with Session(run.engine) as db, db.begin():
            db.execute(delete(Account).where(Account.id == run.uid))
        run.engine.dispose()
        run.report.update(
            temporary_account_removed=True, finished_at_utc=datetime.now(UTC).isoformat()
        )
        run.report["summary"] = summarize_skill(run.report)
        run.save()
        (folder / "summary.json").write_text(
            json.dumps(run.report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "folder": str(folder),
                    "status": run.report["status"],
                    "summary": run.report["summary"],
                }
            ),
            flush=True,
        )
    return run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.folder = args.folder.resolve()
    args.folder.relative_to(ROOT / "runtime/reports/real-chat-prompt")
    if args.live:
        asyncio.run(resume(args.folder))
