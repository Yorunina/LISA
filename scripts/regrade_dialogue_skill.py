"""Regrade a preserved development output set after a documented grading transport repair."""

import argparse
import asyncio
import copy
import json
import random
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.models import Account  # noqa: E402
from scripts.benchmark_chat_prompt import scored_rows, sha  # noqa: E402
from scripts.benchmark_dialogue_skill import DATA, SkillRun, summarize_skill  # noqa: E402


async def regrade(folder):
    original_file = folder / "results.json"
    original = json.loads(original_file.read_text(encoding="utf-8"))
    assert original["stage"] == "develop" and len(original["pairs"]) == 40
    assert len(original["generations"]) == 20
    args = SimpleNamespace(
        stage="develop",
        modes=[original["draft_mode"]],
        max_calls=30,
        max_cost=Decimal("4"),
        judge_output_tokens=6000,
        model=original["model"],
        reuse_baseline=None,
        prompt=ROOT / "lisa/skills/natural-dialogue/SKILL.md",
    )
    datasets = [
        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
        for n in ("develop", "confirm", "guards")
    ]
    run = SkillRun(args, *datasets)
    execution = original["execution_model_calls"]
    assert sha((ROOT / execution["path"]).read_bytes()) == execution["sha256"]
    run.freeze_model_calls(ROOT / execution["path"])
    for file in (Path(__file__).resolve(), original_file):
        key = str(file.relative_to(ROOT))
        run.hashes[key] = sha(file.read_bytes())
        target = run.folder / "inputs" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes())
    run.report.update(
        experiment="natural_dialogue_skill_review_regrade_v1",
        draft_mode=original["draft_mode"],
        modes=original["modes"],
        regraded_from=original["run_id"],
        source_output_sha256=sha(original_file.read_bytes()),
        pairs=copy.deepcopy(original["pairs"]),
        generations=copy.deepcopy(original["generations"]),
        drafts=copy.deepcopy(original["drafts"]),
        effective_provider_calls_per_turn=2,
        execution_hashes=dict(run.hashes),
    )
    with Session(run.engine) as db, db.begin():
        user = Account(username="skillregrade_" + run.folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
    try:
        await run.calibrate()
        rows = list(run.report["pairs"])
        random.Random(20261028).shuffle(rows)
        for start in range(0, len(rows), 2):
            await run.grade(rows[start : start + 2], "primary")
            if run.report.get("source_drift") or run.report.get("upstream_circuit_open"):
                break
        run.report["status"] = (
            "completed" if len(scored_rows(run.report)) == len(rows) else "partial"
        )
    except Exception as exc:
        run.report.update(status="partial", error=type(exc).__name__, stop_reason=str(exc)[:150])
    finally:
        with Session(run.engine) as db, db.begin():
            db.execute(delete(Account).where(Account.id == run.uid))
        run.engine.dispose()
        run.report.update(
            temporary_account_removed=True, finished_at_utc=datetime.now(UTC).isoformat()
        )
        run.report["summary"] = summarize_skill(run.report)
        run.save()
        (run.folder / "summary.json").write_text(
            json.dumps(run.report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"folder": str(run.folder), "summary": run.report["summary"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.folder = args.folder.resolve()
    args.folder.relative_to(ROOT / "runtime/reports/real-chat-prompt")
    if args.live:
        asyncio.run(regrade(args.folder))
