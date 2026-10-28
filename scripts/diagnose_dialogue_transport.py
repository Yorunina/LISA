"""Synthetic binding and JSON diagnostics with private, unchanged provider text."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.orm import Session

from lisa.chat import _chat_instructions
from lisa.dialogue import Conversation
from lisa.models import Account, Persona, Task
from scripts.benchmark_dialogue_skill import DATA, ROOT, SKILL_ROOT, SkillRun, sha


async def diagnose(output_budget=False):
    args = SimpleNamespace(
        stage="develop",
        modes=["corpus"],
        max_calls=10,
        max_cost=Decimal("3"),
        model=None,
        reuse_baseline=None,
        prompt=SKILL_ROOT / "SKILL.md",
    )
    data = [
        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
        for n in ("develop", "confirm", "guards")
    ]
    run = SkillRun(args, *data)
    path = Path(__file__).resolve()
    run.hashes[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    run.report.update(experiment="dialogue_transport_binding_v1", diagnostics=[], raw_responses=[])
    with Session(run.engine) as db, db.begin():
        user = Account(username="skilldiagnostic_" + run.folder.name[-6:], password_hash="!")
        db.add(user)
        db.flush()
        run.uid = user.id
        persona = Persona(user_id=user.id, name="合成", prompt="自然直率。", version=1)
        task = Task(
            user_id=user.id,
            kind="model_probe",
            source="development",
            status="completed",
            request={},
        )
        db.add_all([persona, task])
        db.flush()
        ordinary = "刚收拾完桌子，总算能把杯子放下了。"
        instructions = _chat_instructions(
            db, user.id, task, persona, [ordinary], dialogue_skill_mode="review"
        )
    original_provider = run._frozen_model_calls._provider_request

    async def capture(*args, **kwargs):
        response = await original_provider(*args, **kwargs)
        choices = getattr(response, "choices", None)
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        run.report["raw_responses"].append(
            {
                "status": getattr(response, "status", None),
                "text": getattr(message, "content", None)
                if choice
                else getattr(response, "output_text", None),
                "finish_reason": getattr(choice, "finish_reason", None),
                "reasoning_chars": len(getattr(message, "reasoning_content", "") or ""),
                "endpoint": kwargs.get("endpoint"),
            }
        )
        run.save()
        return response

    run._frozen_model_calls._provider_request = capture
    try:
        for kind in () if output_budget else ("instructions", "system", "developer", "last_user"):
            marker = "LISA_BIND_" + kind.upper() + "_7C92"
            directive = "仅输出标记 " + marker + "，不要标点或解释。"
            messages = [{"role": "user", "content": "你好，随便回应一句。"}]
            prompt = directive if kind == "instructions" else ""
            if kind in {"system", "developer"}:
                messages.insert(0, {"role": kind, "content": directive})
            if kind == "last_user":
                messages.append({"role": "user", "content": directive})
            value = await run.call("binding/" + kind, prompt, messages)
            run.report["diagnostics"].append(
                {
                    "kind": kind,
                    "expected": marker,
                    "received": value.text,
                    "matched": value.text == marker,
                }
            )
            run.save()
        variants = (
            [("native", 600), ("native_large", 1800), ("native_budget", 4096)]
            if output_budget
            else [("native", 600), ("system", 600), ("last_user", 600)]
        )
        for kind, tokens in variants:
            messages = [{"role": "user", "content": ordinary}]
            if kind == "system":
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": Conversation.structured_instructions(instructions),
                    },
                )
            if kind == "last_user":
                messages.append(
                    {"role": "user", "content": Conversation.structured_instructions("")}
                )
            record = {"kind": "structured_" + kind}
            try:
                value = await run.call(record["kind"], instructions, messages, Conversation, tokens)
                record.update(parsed=True, decision=value.model_dump(mode="json"))
            except Exception as exc:
                record.update(parsed=False, error=type(exc).__name__)
            run.report["diagnostics"].append(record)
            run.save()
        run.report["status"] = "completed"
    except Exception as exc:
        run.report.update(status="partial", error=type(exc).__name__)
    finally:
        with Session(run.engine) as db, db.begin():
            db.execute(delete(Account).where(Account.id == run.uid))
        run.engine.dispose()
        run.report.update(
            temporary_account_removed=True, finished_at_utc=datetime.now(UTC).isoformat()
        )
        run.save()
        print(
            json.dumps(
                {"folder": str(run.folder), "diagnostics": run.report["diagnostics"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
    return run
