"""Two explicitly requested synthetic writing calls through the shared development budget."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lisa.author import generate_writing
from lisa.db import make_engine
from lisa.models import Account, ModelCall, Task
from lisa.settings import ROOT, Settings
from lisa.works import document_text, paragraph_document, replace_fragment


async def probe(username: str) -> bool:
    settings, records = Settings(), []
    engine = make_engine(settings)
    instructions = (
        "你是小说作者，只完成一次有限任务，不输出分析或草稿。"
        "scene输出约150字的一个短场景，rewrite只输出指定原文片段的替换正文，最多120字。"
        "新明确约束优先。paragraphs为正式正文，summary和plot_state各不超过60字且只记已发生的事。"
    )
    materials = {
        "title": "雨站来信",
        "agreement": "雨夜，林舟在废弃车站收到一封匿名信。",
        "worldbook": "现实世界，没有魔法。",
        "constraints": "林舟不拆信，不出现超自然现象。",
        "kind": "scene",
        "goal": "写发现信的一幕，结尾停在拿起信封。",
    }
    try:
        with Session(engine) as db:
            user = db.scalar(select(Account).where(Account.username == username, Account.active))
            if user is None:
                raise ValueError("An existing active test account is required")
            user_id = user.id
        original, target, prefix = None, "", ""
        for kind in ("scene", "rewrite"):
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
            result, error = None, None
            try:
                result = await generate_writing(
                    settings,
                    engine,
                    task_id,
                    user_id,
                    instructions,
                    [{"role": "user", "content": json.dumps(materials, ensure_ascii=False)}],
                )
                if sum(map(len, result.paragraphs)) > settings.writing_max_chars:
                    raise ValueError("Writing scope exceeded")
                if kind == "rewrite":
                    rewritten = replace_fragment(original, target, result.paragraphs)
                    assert document_text(rewritten) == prefix + "\n".join(result.paragraphs)
            except Exception as exc:
                error = type(exc).__name__
            with Session(engine) as db, db.begin():
                task = db.get(Task, task_id)
                task.status, task.error = ("failed" if error else "completed"), error
                task.finished_at = datetime.now(UTC)
                calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
                records.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "task_id": str(task_id),
                        "kind": kind,
                        "model": settings.model_name,
                        "synthetic_input": dict(materials),
                        "result": result.model_dump() if result else None,
                        "error": error,
                        "calls": [
                            {
                                "state": c.state,
                                "input_tokens": c.input_tokens,
                                "output_tokens": c.output_tokens,
                                "estimated_usd": str(c.charged_usd),
                            }
                            for c in calls
                        ],
                    }
                )
            if error:
                break
            if kind == "scene":
                original = paragraph_document(result.paragraphs)
                target = result.paragraphs[-1]
                prefix = document_text(original)[: -len(target)]
                materials.update(
                    kind="rewrite",
                    constraints="新明确要求：林舟拆开信，信里只有一张空白纸。没有超自然现象。",
                    goal="只重写末段，落实新要求。",
                    target_text=target,
                    context=document_text(original),
                    summary=result.summary,
                    plot_state=result.plot_state,
                )
    finally:
        engine.dispose()
        report = ROOT / "runtime" / "reports" / "probes" / "writing-probe.jsonl"
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    success = len(records) == 2 and all(record["error"] is None for record in records)
    print(
        json.dumps(
            {
                "success": success,
                "calls": len(records),
                "report": "runtime/reports/probes/writing-probe.jsonl",
            }
        )
    )
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(probe(args.user)) else 1)
