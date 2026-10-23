"""Opt-in synthetic style smoke through the real shared budget, without QQ or user material."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.db import make_engine
from lisa.models import Account, ModelCall, StyleCorpus, Task
from lisa.settings import ROOT, Settings
from lisa.style_corpus import parse_chat, parse_writing
from lisa.style_distillation import execute_style

SAMPLES = {
    "writing": (
        "第一章 雨后\n巷口的水慢慢退了。他将伞靠在门边，听见楼上有人挪动椅子。"
        "等了片刻，他没有敲门。\n\n第二章 灯\n电车驶过窗前，桌上的杯沿闪了一下。"
        "她把未寄出的纸折回信封，随后关掉台灯。"
    ),
    "personality": (
        "2026-10-20 19:00:00 甲\n晚饭做糊了，今天真不顺。\n"
        "2026-10-20 19:00:10 乙\n先把锅救下来，晚饭我们再另想办法。\n"
        "2026-10-20 19:01:00 甲\n哈哈，锅比我还需要安慰。\n"
        "2026-10-20 19:01:10 乙\n毕竟它真的经历了烟火气。\n"
        "2026-10-20 19:02:00 甲\n不过我有件认真事想说。\n"
        "2026-10-20 19:02:10 乙\n你说，我先听完。"
    ),
}


async def probe() -> bool:
    settings = Settings()
    engine = make_engine(settings)
    outcomes = []
    with Session(engine) as db, db.begin():
        user = Account(username="style_probe_" + uuid4().hex[:12], password_hash="no-probe-login")
        db.add(user)
        db.flush()
        user_id = user.id
    try:
        for kind, source in SAMPLES.items():
            if kind == "personality":
                segments, participants, _ = parse_chat(source, 0)
                targets = [participants[1]["id"]]
            else:
                segments, participants, targets = parse_writing(source, 0), [], []
            with Session(engine) as db, db.begin():
                task = Task(
                    user_id=user_id, kind="style_distill", source="development", status="pending"
                )
                db.add(task)
                db.flush()
                task_id = task.id
                corpus = StyleCorpus(
                    user_id=user_id, kind=kind, state="processing", task_id=task_id,
                    payload={"files": [{"name": "synthetic.txt", "text": source,
                                        "encoding": "utf-8", "segments": segments}]},
                    preview={"files": [], "participants": participants, "total_chars": len(source)},
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                db.add(corpus)
                db.flush()
                corpus_id = corpus.id
                task.request = {"corpus_id": str(corpus_id), "kind": kind,
                                "target_speakers": targets, "selection": []}
            await execute_style(settings, engine, task_id)
            with Session(engine) as db:
                task, corpus = db.get(Task, task_id), db.get(StyleCorpus, corpus_id)
                calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
                outcomes.append({
                    "kind": kind, "status": task.status, "error": task.error,
                    "cleaned": not corpus.payload and corpus.state == "cleaned",
                    "result": task.result,
                    "calls": [{"state": c.state, "error": c.error, "input": c.input_tokens,
                               "output": c.output_tokens, "estimated_usd": str(c.charged_usd)}
                              for c in calls],
                })
    finally:
        with Session(engine) as db, db.begin():
            user = db.get(Account, user_id)
            if user is not None:
                db.delete(user)
        engine.dispose()
    report = {"at": datetime.now(UTC).isoformat(), "synthetic": True, "outcomes": outcomes}
    output = ROOT / "runtime" / "style-probe.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    success = all(item["status"] == "completed" and item["cleaned"] for item in outcomes)
    summary = [
        {key: value for key, value in item.items() if key != "result"}
        | {"prompt_chars": len((item["result"] or {}).get("prompt", ""))}
        for item in outcomes
    ]
    print(json.dumps({"success": success, "outcomes": summary}))
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.parse_args()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        raise SystemExit(0 if runner.run(probe()) else 1)
