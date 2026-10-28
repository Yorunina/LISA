"""Development only: isolate a bounded review pass using frozen drafts and untouched references."""

import argparse
import asyncio
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

from lisa.dialogue_skill import SKILL_ROOT, compile_dialogue_skill  # noqa: E402
from lisa.dialogue_skill_review import review_request  # noqa: E402
from lisa.models import Account  # noqa: E402
from lisa.style_distillation import TrialOutput  # noqa: E402
from scripts.benchmark_chat_prompt import messages_for, ordered_check, scored_rows, sha  # noqa: E402
from scripts.benchmark_dialogue_skill import (  # noqa: E402
    DATA,
    GENERATION_FORMAT,
    IDENTITY,
    SkillRun,
    summarize_skill,
)


async def execute(folder, mode, controls=None):
    source = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert source["stage"] == "develop" and source["status"] == "completed"
    args = SimpleNamespace(
        stage="develop",
        modes=[mode],
        max_calls=90,
        max_cost=Decimal("5"),
        model=source["model"],
        reuse_baseline=None,
        prompt=SKILL_ROOT / "SKILL.md",
    )
    data = [
        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
        for n in ("develop", "confirm", "guards")
    ]
    run = SkillRun(args, *data)
    prompt = IDENTITY + compile_dialogue_skill(mode)
    assert sha((prompt + "\n\n" + GENERATION_FORMAT).encode()) == source["wire_prompt_hashes"][mode]
    assert run.settings.model_base_url == source["base_url"]
    assert run.settings.model_reasoning_effort == source["reasoning"]
    assert run.settings.model_max_output_tokens == source["generation_output_tokens"]
    run.freeze_model_calls(folder / "inputs/lisa/model_calls.py")
    for file in (Path(__file__).resolve(), ROOT / "lisa/dialogue_skill_review.py"):
        key = str(file.relative_to(ROOT))
        run.hashes[key] = sha(file.read_bytes())
        target = run.folder / "inputs" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes())
    run.report.update(
        experiment="natural_dialogue_skill_review_v1",
        draft_mode=mode,
        draft_source_run=source["run_id"],
        drafts={},
        modes=["review"],
        execution_hashes=dict(run.hashes),
        effective_provider_calls_per_turn=2,
    )
    controls = controls or ["previous", mode]
    run.report["controls"] = controls
    review_text = (SKILL_ROOT / "references/review.md").read_text(encoding="utf-8")
    with Session(run.engine) as db, db.begin():
        user = Account(username="skillreview_" + run.folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
    try:
        await run.calibrate()
        for kind, cases in (("real", data[0]["cases"]), ("guard", data[2]["cases"])):
            for case in random.Random("review-" + kind).sample(cases, len(cases)):
                prior, output = [], {"turns": []}
                for turn, text in enumerate(case["turns"]):
                    key = f"{kind}/{case['id']}/r0/{mode}"
                    messages = messages_for(case, prior, text)
                    if len(case["turns"]) == 1:
                        draft = TrialOutput(text=source["generations"][key]["turns"][turn])
                        origin = source["run_id"]
                    else:
                        draft = await run.call(
                            "workflow/draft/" + key + f"/t{turn}",
                            prompt + "\n\n" + GENERATION_FORMAT,
                            messages,
                        )
                        origin = run.folder.name
                    run.report["drafts"][key + f"/t{turn}"] = {
                        "text": draft.text,
                        "source_run": origin,
                        "history": prior.copy(),
                    }
                    instructions, request = review_request(prompt, messages, review_text, draft)
                    value = await run.call(
                        "workflow/review/" + key + f"/t{turn}",
                        instructions + "\n\n" + GENERATION_FORMAT,
                        request,
                    )
                    output["turns"].append(value.text)
                    prior.extend(
                        [
                            {"role": "user", "content": text},
                            {"role": "assistant", "content": value.text},
                        ]
                    )
                output["ordered_check"] = ordered_check(case, output["turns"])
                run.report["generations"][f"{kind}/{case['id']}/r0/review"] = output
                for control in controls:
                    group = "review_vs_" + control + "_" + kind
                    run.report["pairs"].append(
                        {
                            "id": group + "/" + case["id"],
                            "group": group,
                            "case": case,
                            "rep": 0,
                            "variant": "candidate",
                            "outputs": {
                                "baseline": source["generations"][
                                    f"{kind}/{case['id']}/r0/{control}"
                                ],
                                "candidate": output,
                            },
                        }
                    )
                run.save()
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
    return run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--draft-mode", choices=("core", "corpus"), required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.folder = args.folder.resolve()
    args.folder.relative_to(ROOT / "runtime/reports/real-chat-prompt")
    if args.live:
        asyncio.run(execute(args.folder, args.draft_mode))
