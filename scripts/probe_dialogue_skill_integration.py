"""Six isolated structured-output probes; parsed decisions are never applied or sent."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.chat import _chat_instructions, _message_parts  # noqa: E402
from lisa.dialogue import Conversation, Dialogue  # noqa: E402
from lisa.models import Account, Persona, Task  # noqa: E402
from scripts.benchmark_chat_prompt import sha  # noqa: E402
from scripts.benchmark_dialogue_skill import DATA, SkillRun  # noqa: E402


async def probe(mode):
    args = SimpleNamespace(
        stage="develop",
        modes=[mode],
        max_calls=16 if mode == "review" else 8,
        max_cost=Decimal("2"),
        model=None,
        reuse_baseline=None,
        prompt=ROOT / "lisa/skills/natural-dialogue/SKILL.md",
    )
    datasets = [
        json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("develop", "confirm", "guards")
    ]
    run = SkillRun(args, *datasets)
    run.report["generation_output_tokens"] = run.settings.dialogue_skill_output_tokens
    for file in (
        Path(__file__).resolve(),
        ROOT / "lisa/chat.py",
        ROOT / "lisa/dialogue.py",
        ROOT / "lisa/life.py",
        ROOT / "lisa/memory.py",
        ROOT / "lisa/structured_output.py",
    ):
        name = str(file.relative_to(ROOT))
        run.hashes[name] = sha(file.read_bytes())
        target = run.folder / "inputs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes())
    run.report.update(
        experiment="natural_dialogue_skill_integration_v1",
        stage="integration",
        probes=[],
        schema="Conversation and Dialogue",
        protocol=run.settings.model_api_protocol,
        source_sha256=run.hashes,
    )
    cases = [
        {"id": "ordinary", "turns": ["刚收拾完桌子，总算能把杯子放下了。"]},
        *[c for c in datasets[2]["cases"] if c["id"] in {"G01", "G02", "H09", "H10"}],
        datasets[0]["cases"][0],
        {"id": "creative_ordinary", "turns": ["先聊聊方向，不要开始写。"], "creative": True},
        {
            "id": "creative_rewrite",
            "turns": ["帮我把‘下雨了’改得具体一点，只要一句。"],
            "creative": True,
        },
    ]
    original_provider = run._frozen_model_calls._provider_request
    run.report["wire_checks"] = []
    active_schema, active_case = Conversation, ""

    async def inspect(*args, **kwargs):
        response = await original_provider(*args, **kwargs)
        normalized = (
            run._frozen_model_calls._completion_response(response)
            if kwargs.get("endpoint") == "chat.completions"
            else response
        )
        record = {
            "case": active_case,
            "schema": active_schema.__name__,
            "endpoint": kwargs["endpoint"],
        }
        try:
            raw = json.loads(normalized.output_text)
            active_schema.model_validate(raw, strict=True)
            record.update(
                strict_valid=True,
                all_fields=set(raw) == set(active_schema.model_fields),
                intervals_match=len(raw.get("messages", []))
                == len(raw.get("message_intervals", [])),
            )
        except Exception as exc:
            record.update(strict_valid=False, error=type(exc).__name__)
        run.report["wire_checks"].append(record)
        run.save()
        return response

    run._frozen_model_calls._provider_request = inspect
    with Session(run.engine) as db, db.begin():
        user = Account(username="skillprobe_" + run.folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
        db.add(
            Persona(
                user_id=user.id, name="隔离测试LISA", prompt="自然直率，有自己的看法。", version=1
            )
        )
    try:
        for case in cases:
            active_schema = Dialogue if case.get("creative") else Conversation
            active_case = case["id"]
            with Session(run.engine) as db, db.begin():
                from sqlalchemy import select

                persona = db.scalar(select(Persona).where(Persona.user_id == run.uid))
                task = Task(
                    user_id=run.uid,
                    kind="model_probe",
                    source="development",
                    status="completed",
                    request={},
                )
                db.add(task)
                db.flush()
                instructions = _chat_instructions(
                    db, run.uid, task, persona, case["turns"], dialogue_skill_mode=mode
                )
                snapshot = task.input_snapshot["dialogue_guide"]
            record = {"id": case["id"], "guide_sha256": snapshot["sha256"]}
            try:
                value = await run.call(
                    "integration/" + case["id"],
                    instructions,
                    [
                        *case.get("history", []),
                        {"role": "user", "content": "\n".join(case["turns"])},
                    ],
                    active_schema,
                    run.settings.dialogue_skill_output_tokens,
                )
                record["decision"] = value.model_dump(mode="json")
                record["parsed"] = True
                record["delivery_parts"] = _message_parts(value, value.reply)
            except Exception as exc:
                record.update(parsed=False, error=type(exc).__name__)
            run.report["probes"].append(record)
            run.save()
            if run.report.get("upstream_circuit_open"):
                break
        run.report["status"] = (
            "completed"
            if len(run.report["probes"]) == len(cases)
            and all(p["parsed"] for p in run.report["probes"])
            else "partial"
        )
    finally:
        with Session(run.engine) as db, db.begin():
            db.execute(delete(Account).where(Account.id == run.uid))
        run.report.update(
            temporary_account_removed=True, finished_at_utc=datetime.now(UTC).isoformat()
        )
        run.save()
        run.engine.dispose()
        print(
            json.dumps(
                {
                    "folder": str(run.folder),
                    "status": run.report["status"],
                    "parsed": sum(p["parsed"] for p in run.report["probes"]),
                    "total": len(run.report["probes"]),
                }
            ),
            flush=True,
        )
    return run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("core", "examples", "corpus", "review"), required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        asyncio.run(probe(args.mode))
