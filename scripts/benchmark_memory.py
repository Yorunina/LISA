"""Small synthetic memory-retrieval comparison; offline unless --live is explicit."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.chat_context import recall_query  # noqa: E402
from lisa.db import make_engine  # noqa: E402
from lisa.model_calls import generate_embeddings  # noqa: E402
from lisa.models import Account, Memory, ModelCall, Project, Task  # noqa: E402
from lisa.observability import finalize_context  # noqa: E402
from lisa.recall import retrieve_context  # noqa: E402
from lisa.retrieval import ensure_index_tasks, execute_index  # noqa: E402
from lisa.settings import Settings  # noqa: E402
from scripts.benchmark_rag import _mean, _pack_final  # noqa: E402

GOLD = ROOT / "tests/fixtures/memory_retrieval_gold_v1.json"
ARMS = (("baseline", False, 1), ("context", True, 1), ("context_candidates", True, 3))


def load_gold():
    data = json.loads(GOLD.read_text(encoding="utf-8"))
    cases = data["cases"]
    if not 20 <= len(cases) <= 30 or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Expected 20-30 uniquely identified synthetic cases")
    for documents in data["corpora"].values():
        if any(len(item["content"]) * item.get("repeat", 1) > 2000 for item in documents):
            raise ValueError("Synthetic memory exceeds the normal 2000-character write limit")
    for case in cases:
        ids = {item["id"] for item in data["corpora"][case["corpus"]]}
        if not set(case["expected"]).issubset(ids):
            raise ValueError("Expected memory must belong to the case corpus")
    return data


def _history(case, now):
    return [{"role": item["role"], "content": item["content"],
             "time": (now - timedelta(minutes=item["age_minutes"])).isoformat()}
            for item in case["history"]]


def _scores(expected, relevant, candidates, final):
    expected, relevant = set(expected), set(relevant)
    top = set(candidates[:5])
    return {
        "recall_at_5": len(expected & top) / len(expected) if expected else None,
        "precision_at_5": len(relevant & top) / len(top) if top else None,
        "pool_coverage": len(expected.intersection(candidates)) / len(expected)
        if expected else None,
        "context_coverage": len(expected.intersection(final)) / len(expected)
        if expected else None,
        "no_answer_candidate_count": len(candidates) if not expected else None,
    }


def _summary(rows):
    return {"sample_count": len(rows), **{
        key: _mean([row[key] for row in rows]) for key in (
            "recall_at_5", "precision_at_5", "pool_coverage", "context_coverage",
            "no_answer_candidate_count",
        )}}


def _ledger(engine):
    with Session(engine) as db:
        calls = list(db.scalars(select(ModelCall)))
        return {"call_count": len(calls),
                "charged_cny": str(sum((call.charged_usd for call in calls
                                         if call.charged_usd is not None), start=0)),
                "unsettled_call_count": sum(call.charged_usd is None for call in calls),
                "input_tokens": sum(call.input_tokens or 0 for call in calls),
                "states": {state: sum(call.state == state for call in calls)
                           for state in sorted({call.state for call in calls})}}


async def run_comparison(settings, engine, data):
    """Real embeddings and production indexing/recall; no generation or QQ transport."""
    import lisa.retrieval as retrieval

    if not re.fullmatch(r"lisa_test_[0-9a-f]{32}", settings.db_name):
        raise ValueError("A disposable lisa_test database is required")
    now, memories, projects, tasks = datetime.now(UTC), {}, {}, {}
    with Session(engine) as db, db.begin():
        user = Account(username="memory_benchmark", password_hash="unused-synthetic")
        db.add(user)
        db.flush()
        user_id = user.id
        general = Project(user_id=user_id, title="合成共同资料", is_current=False)
        db.add(general)
        db.flush()
        for name, documents in data["corpora"].items():
            project = general
            if name in {"crowded", "budget"}:
                project = Project(user_id=user_id, title="合成资料 " + name, is_current=False)
                db.add(project)
                db.flush()
            projects[name] = project.id
            for document in documents:
                item = Memory(user_id=user_id, project_id=project.id, kind="fact",
                              content=document["content"] * document.get("repeat", 1),
                              allow_chat=True, allow_writing=False)
                db.add(item)
                db.flush()
                memories[name, document["id"]] = item.id
        for case in data["cases"]:
            task = Task(user_id=user_id, project_id=projects[case["corpus"]], kind="chat",
                        source="development", status="running")
            db.add(task)
            db.flush()
            tasks[case["id"]] = task.id
    # Indexing remains the same production path. Unknown or failed calls stop this run.
    for _ in range(20):
        with Session(engine) as db, db.begin():
            index_ids = [task.id for task in ensure_index_tasks(db, settings)]
        if not index_ids:
            break
        for task_id in index_ids:
            await execute_index(settings, engine, task_id)
            with Session(engine) as db:
                task = db.get(Task, task_id)
                if task.status != "completed":
                    raise RuntimeError("Index attempt stopped: " + task.status)
    else:
        raise RuntimeError("Index batch limit reached")
    rows = []
    original_multiplier = retrieval.CHAT_MEMORY_CANDIDATE_MULTIPLIER
    try:
        for case in data["cases"]:
            # Reuse only successful vectors from this case's same task across arms.
            # This isolates ranking/query differences without charging identical input twice.
            vectors = {}

            async def embedding(config, db_engine, task_id, owner, texts):
                missing = [text for text in texts if text not in vectors]
                if missing:
                    result = await generate_embeddings(config, db_engine, task_id, owner, missing)
                    vectors.update(zip(missing, result, strict=True))
                return [vectors[text] for text in texts]

            corpus = case["corpus"]
            labels = {str(value): key for (name, key), value in memories.items()
                      if projects[name] == projects[corpus]}
            for arm, with_history, multiplier in ARMS:
                retrieval.CHAT_MEMORY_CANDIDATE_MULTIPLIER = multiplier
                query = recall_query(case["input"], _history(case, now)) if with_history else (
                    case["input"][-1500:]
                )
                context = await retrieve_context(
                    settings, engine, tasks[case["id"]], user_id, projects[corpus], "chat", query,
                    embed=embedding,
                )
                if context.metrics.get("degraded"):
                    raise RuntimeError("Embedding fallback is not semantic evaluation evidence")
                candidates = [labels[key.split(":")[1]]
                              for key in context.metrics.get("candidate_keys", [])]
                context = _pack_final(settings, context, "chat", "memory", case["input"])
                finalize_context(engine, context)
                final = [labels[key.split(":")[1]]
                         for key in context.metrics.get("final_complete_keys", [])]
                rows.append({"id": case["id"], "group": case["group"], "arm": arm,
                             "query_expanded": query != case["input"][-1500:],
                             "query_chars": len(query), "candidate_slice_limit": 5 * multiplier,
                             "eligible_memory_count": len(labels),
                             "returned": candidates, "final": final,
                             "expected": case["expected"],
                             **_scores(case["expected"], case.get("relevant", case["expected"]),
                                       candidates, final),
                             "omitted": context.metrics.get("omitted", {}),
                             "durations_ms": context.metrics.get("durations_ms", {})})
    finally:
        retrieval.CHAT_MEMORY_CANDIDATE_MULTIPLIER = original_multiplier
    return {"status": "completed", "created_at": now.isoformat(),
            "evaluation_kind": "real_embedding_retrieval_only",
            "window_retention": "covered by integration tests, not simulated here",
            "answer_quality": "not evaluated; no chat model calls",
            "embedding_reuse": "successful same-case vectors reused across arms; "
                               "arm timings are not an end-to-end latency comparison",
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "dataset_sha256": hashlib.sha256(GOLD.read_bytes()).hexdigest(),
            "code_sha256": hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in (
                "lisa/chat_context.py", "lisa/retrieval.py", "lisa/recall.py",
                "scripts/benchmark_memory.py",
            ))).hexdigest(),
            "arms": {name: _summary([row for row in rows if row["arm"] == name])
                     for name, *_ in ARMS},
            "by_group": {name: {group: _summary([row for row in rows
                         if row["arm"] == name and row["group"] == group])
                         for group in sorted({row["group"] for row in rows})}
                         for name, *_ in ARMS}, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real embedding in an isolated DB")
    parser.add_argument("--output", default="runtime/reports/memory-retrieval-comparison.json")
    args = parser.parse_args()
    data = load_gold()
    if not args.live:
        print(json.dumps({"status": "offline_validated", "cases": len(data["cases"]),
                          "groups": sorted({case["group"] for case in data["cases"]}),
                          "model_calls": 0, "database_access": False}))
        return
    import psycopg
    from dotenv import dotenv_values
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    env = {**{k: v for k, v in dotenv_values(ROOT / "runtime/.env").items() if v}, **os.environ}
    database = "lisa_test_" + uuid4().hex
    admin_info = make_conninfo(
        host=env["LISA_DB_HOST"], port=env["LISA_DB_PORT"], dbname="postgres",
        user="postgres", password=env["LISA_DB_ADMIN_PASSWORD"], connect_timeout=5,
    )
    child_env = {**env, "LISA_DB_NAME": database, "PYTHONUTF8": "1"}
    report, engine = {"status": "failed"}, None
    with psycopg.connect(admin_info, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier(env["LISA_DB_USER"])))
        with ExitStack() as cleanup:
            cleanup.callback(admin.execute, sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(database)))
            try:
                with psycopg.connect(
                    make_conninfo(admin_info, dbname=database), autocommit=True
                ) as db:
                    db.execute("CREATE EXTENSION IF NOT EXISTS vector")
                migrated = subprocess.run([sys.executable, "-m", "lisa", "migrate"], cwd=ROOT,
                                          env=child_env, capture_output=True, text=True)
                if migrated.returncode:
                    raise RuntimeError("Isolated migration failed")
                settings = Settings(db_name=database)
                engine = make_engine(settings)
                cleanup.callback(engine.dispose)
                report = asyncio.run(run_comparison(settings, engine, data))
            except Exception as exc:
                report["failure_type"] = type(exc).__name__
            finally:
                if engine:
                    report["ledger"] = _ledger(engine)
        report["isolated_database_removed"] = True
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"],
                      "ledger": report.get("ledger"), "arms": report.get("arms")}))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
