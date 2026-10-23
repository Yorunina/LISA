"""Explicit, bounded live model check with synthetic dialogue and real usage records."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.db import make_engine
from lisa.dialogue import creative_context, generate_dialogue
from lisa.model_calls import generate_reply
from lisa.models import Account, ModelCall, Task
from lisa.settings import ROOT, Settings


async def probe(username: str, turns: int, *, creative: bool = False) -> bool:
    settings = Settings()
    engine = make_engine(settings)
    inputs = [
        "我今天想写一个发生在雨夜车站的故事，但先只聊聊方向，别开始写正文。",
        "我刚才提到的故事地点是什么？我想让气氛更安静一点，先别写正文。",
    ]
    messages = []
    instructions = (
        "这是 LISA 的合成对话验收，不是正式人格。用简短自然的中文回应，少用反问，"
        "不固定夸赞，不催用户填问卷。允许只聊创意，不擅自开始写正文。"
    )
    if creative:
        inputs = [
            "确定了：故事设在雨夜车站，主角收到匿名信。请写他发现信的一个短场景，停在拿起信封。"
        ]
        instructions += creative_context(
            SimpleNamespace(
                title="雨站来信",
                kind="novel",
                completed=False,
                is_current=True,
                agreement="",
                constraints="现实世界，没有超自然现象。",
                summary="",
                preferences="",
                tentative="",
            )
        )
    records = []
    success = True
    try:
        with Session(engine) as db:
            user = db.scalar(select(Account).where(Account.username == username, Account.active))
            if user is None:
                raise ValueError("An existing active test account is required")
            user_id = user.id
        for content in inputs[:turns]:
            with Session(engine) as db, db.begin():
                task = Task(
                    user_id=user_id,
                    kind="model_probe",
                    source="development",
                    status="running",
                    started_at=datetime.now(UTC),
                )
                db.add(task)
                db.flush()
                task_id = task.id
            messages.append({"role": "user", "content": content})
            reply = None
            decision = None
            error = None
            try:
                result = await (generate_dialogue if creative else generate_reply)(
                    settings, engine, task_id, user_id, instructions, messages
                )
                if creative:
                    decision, reply = result.model_dump(), result.reply
                    if (
                        result.commission is None
                        or not result.commission.requested
                        or result.commission.evidence not in content
                    ):
                        raise ValueError("Explicit finite commission was not grounded")
                else:
                    reply = result
                messages.append({"role": "assistant", "content": reply})
            except Exception as exc:
                error = type(exc).__name__  # Never print provider errors or credentials.
                success = False
            with Session(engine) as db, db.begin():
                task = db.get(Task, task_id)
                task.status, task.error = ("completed" if reply and not error else "failed"), error
                task.finished_at = datetime.now(UTC)
                calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
                record = {
                    "at": datetime.now(UTC).isoformat(),
                    "task_id": str(task_id),
                    "model": settings.model_name,
                    "synthetic_input": content,
                    "reply": reply,
                    "decision": decision,
                    "error": error,
                    "calls": [
                        {
                            "state": c.state,
                            "input_tokens": c.input_tokens,
                            "output_tokens": c.output_tokens,
                            "estimated_usd": str(c.charged_usd),
                            "error": c.error,
                        }
                        for c in calls
                    ],
                }
            records.append(record)
            if error:
                break
    finally:
        engine.dispose()
        report = ROOT / "runtime" / "model-probe.jsonl"
        report.parent.mkdir(exist_ok=True)
        with report.open("a", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {"success": success, "turns": len(records), "report": "runtime/model-probe.jsonl"}
        )
    )
    return success


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--turns", type=int, choices=[1, 2], default=2)
    parser.add_argument(
        "--creative", action="store_true", help="Validate one synthetic finite writing commission"
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(probe(args.user, args.turns, creative=args.creative)) else 1)


if __name__ == "__main__":
    main()
