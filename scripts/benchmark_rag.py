"""Repeatable production retrieval regression in a disposable database; no model or QQ calls."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import SecretStr  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from lisa.db import make_engine  # noqa: E402
from lisa.models import (  # noqa: E402
    Account,
    Chapter,
    ChapterVersion,
    Character,
    Memory,
    Project,
    RagEvaluationRun,
    Task,
)
from lisa.observability import finalize_context  # noqa: E402
from lisa.recall import retrieve_context  # noqa: E402
from lisa.retrieval import embedding_space, ensure_index_tasks, execute_index  # noqa: E402
from lisa.settings import Settings  # noqa: E402

GOLD = ROOT / "tests" / "fixtures" / "rag_dashboard_gold_v1.json"


def _vector(value):
    match = re.search(r"航标(\d+)", value)
    index = int(match.group(1)) % 16 if match else 15
    return [float(position == index) for position in range(16)]


async def synthetic_embedding(settings, engine, task_id, user_id, texts):
    return [_vector(text) for text in texts]


def _mean(values):
    selected = [value for value in values if value is not None]
    return sum(selected) / len(selected) if selected else None


def _summary(rows):
    return {
        "sample_count": len(rows),
        "answerable_count": sum(row["recall_at_k"] is not None for row in rows),
        "recall_at_k": _mean([row["recall_at_k"] for row in rows]),
        "precision_at_k": _mean([row["precision_at_k"] for row in rows]),
        "context_coverage": _mean([row["context_coverage"] for row in rows]),
        "passed_count": sum(row["passed"] for row in rows),
    }


def _seed(db, case, prefix):
    user = Account(username=f"rb_{prefix}_{case['id']}", password_hash="unused-synthetic")
    other = Account(username=f"rf_{prefix}_{case['id']}", password_hash="unused-synthetic")
    db.add_all([user, other])
    db.flush()
    project = Project(user_id=user.id, title="合成基准作品", kind="novel",
                      retrieval_config={"sources": ["memory"], "limit": 5, "max_chars": 1500})
    other_project = Project(user_id=user.id, title="合成其他作品", is_current=False)
    db.add_all([project, other_project])
    db.flush()
    scenario, purpose = case["scenario"], case["purpose"]
    marker = re.search(r"航标\d+", case["question"]).group(0)
    body = f"{marker}的保存信息为：旧桥在周末开放。"
    if scenario == "duplicate_slices":
        body += "背景说明。" * 140
    elif scenario == "long_budget":
        body += "背景说明。" * 350
    if scenario == "long_budget":
        project.retrieval_config = {"sources": ["memory"], "limit": 5, "max_chars": 300}
    char = Character(project_id=project.id, name="合成角色", prompt="仅使用已知信息。",
                     enabled=True, known_information=body,
                     retrieval_config={"enabled": True, "limit": 5,
                                       "max_chars": 300 if scenario == "long_budget" else 1500})
    db.add(char)
    db.flush()
    expected = []
    hidden = []
    forbidden_text = []
    if scenario.startswith("worldbook_"):
        entry_id = str(uuid4())
        content = body
        if scenario == "worldbook_middle":
            # Only the later slice carries the query marker; it must select
            # the same complete entry as its duplicate/background slices.
            content = "合成前半背景。" * 110 + body + "合成后半背景。" * 70
        project.worldbook_entries = [{
            "id": entry_id, "title": "灯塔值守条例", "category": "rule",
            "content": content, "scope": "仅用于合成基准", "related_ids": [],
        }]
        project.retrieval_config = {"sources": ["project"], "limit": 5, "max_chars": 6000}
        expected = [f"project:{project.id}:worldbook_entry:{entry_id}"]
    elif scenario.startswith("chapter_"):
        from lisa.works import paragraph_document

        chapter = Chapter(project_id=project.id, title="合成当前章节", position=1,
                          version=2, content=paragraph_document([body]))
        db.add(chapter)
        db.flush()
        old_marker = "SYNTHETIC_SUPERSEDED_CHAPTER"
        db.add(ChapterVersion(chapter_id=chapter.id, version=1, title="合成旧章节",
                              content=paragraph_document([old_marker + body]), source="user"))
        forbidden_text.append(old_marker)
        project.retrieval_config = {"sources": ["chapter"], "limit": 5, "max_chars": 1500}
        expected = [f"chapter:{chapter.id}:chapter"]
    elif purpose == "role":
        if case["target_count"]:
            expected = [f"character:{char.id}:known_information"]
        if scenario == "no_sources":
            char.known_information = ""
        elif scenario == "disabled":
            char.retrieval_config = {"enabled": False}
        elif scenario == "no_answer":
            char.known_information = "另一个不相关的合成概念。"
        secret = Character(project_id=project.id, name="其他角色", prompt="合成。",
                           enabled=True, known_information=body)
        db.add(secret)
        db.flush()
        hidden.append(str(secret.id))
    else:
        count = case["target_count"]
        for position in range(count):
            item = Memory(user_id=user.id, project_id=project.id if purpose == "writing" else None,
                          kind="fact", content=body + str(position), allow_chat=True,
                          allow_writing=True)
            db.add(item)
            db.flush()
            expected.append(f"memory:{item.id}:memory")
        if scenario == "disabled":
            blocked = Memory(user_id=user.id, kind="fact", content=body,
                             allow_chat=False, allow_writing=False)
            db.add(blocked)
        elif scenario == "no_answer":
            db.add(Memory(user_id=user.id, kind="fact", content="另一概念的合成资料。",
                          allow_chat=True, allow_writing=True))
        for owner, work in [(other.id, None), (user.id, other_project.id)]:
            item = Memory(user_id=owner, project_id=work, kind="fact", content=body,
                          allow_chat=True, allow_writing=True)
            db.add(item)
            db.flush()
            hidden.append(str(item.id))
    task = Task(user_id=user.id, project_id=project.id,
                kind="chat" if purpose == "chat" else "scene",
                status="running", source="development")
    db.add(task)
    db.flush()
    return {"task": task.id, "user": user.id, "other_user": other.id,
            "project": project.id, "character": char.id,
            "expected": expected, "hidden": hidden, "forbidden_text": forbidden_text}


async def _index_all(settings, engine):
    for _ in range(60):
        with Session(engine) as db, db.begin():
            ids = [task.id for task in ensure_index_tasks(db, settings)]
        if not ids:
            return
        for task_id in ids:
            await execute_index(settings, engine, task_id, embed=synthetic_embedding)
    raise RuntimeError("Synthetic index did not finish")


def _pack_final(settings, context, purpose, scenario, question):
    # Exercise the same request builders/packing as the actual consumers.
    from lisa.author import _pack_author_messages
    from lisa.chat import _trim_chat_history
    from lisa.dialogue import Conversation
    from lisa.model_calls import ModelInputTooLarge

    capacity = 1_000 if scenario == "request_budget" else settings.model_max_input_bytes
    measured = settings.model_copy(update={"model_max_input_bytes": capacity})
    messages = [{"role": "user", "content": question}]
    if scenario == "request_budget":
        # Optional recalled text competes with required current task input.
        messages[0]["content"] += "合成必需输入。" * 60
    try:
        if purpose == "writing":
            messages = [{"role": "user", "content": json.dumps({
                "title": "合成作品", "kind": "novel", "goal": messages[0]["content"],
                "context": "合成当前正文。",
            }, ensure_ascii=False)}]
            _pack_author_messages(measured, "合成作者规则。" + context.text, messages)
        elif purpose == "chat":
            _trim_chat_history(measured, "合成交流规则。" + context.text, messages, [],
                               schema=Conversation)
    except ModelInputTooLarge:
        return context.discarded("request_budget")
    return context


def _code_fingerprint():
    digest = hashlib.sha256()
    for relative in [
        "lisa/retrieval.py", "lisa/recall.py", "lisa/observability.py", "lisa/chat.py",
        "lisa/author.py", "lisa/character_calls.py", "lisa/model_calls.py",
        "scripts/benchmark_rag.py",
    ]:
        digest.update(relative.encode())
        digest.update((ROOT / relative).read_bytes())
    return digest.hexdigest()


def _source_snapshot(db, fixtures):
    snapshot = []
    for seeded in fixtures:
        user_id, project_id = seeded["user"], seeded["project"]
        work = db.get(Project, project_id)
        character = db.get(Character, seeded["character"])
        memories = list(db.scalars(select(Memory).where(
            Memory.user_id.in_([user_id, seeded["other_user"]])
        )
                                  .order_by(Memory.content, Memory.revision)))
        chapters = list(db.scalars(select(Chapter).where(Chapter.project_id == project_id)
                                  .order_by(Chapter.position)))
        snapshot.append({
            "project_revision": work.index_revision, "worldbook": work.worldbook,
            "entries": [
                {k: value for k, value in entry.items() if k not in {"id", "related_ids"}}
                for entry in work.worldbook_entries
            ],
            "retrieval_config": work.retrieval_config,
            "character": {"body": character.known_information, "revision": character.revision,
                          "enabled": character.enabled, "config": character.retrieval_config},
            "memories": sorted([{"body": item.content, "revision": item.revision,
                          "owner": "current" if item.user_id == user_id else "foreign",
                          "chat": item.allow_chat, "writing": item.allow_writing,
                          "scope": "current" if item.project_id == project_id else
                          "global" if item.project_id is None else "other"}
                         for item in memories],
                         key=lambda value: json.dumps(value, sort_keys=True)),
            "chapters": [{"body": item.content, "version": item.version,
                          "historical_versions": [
                              {"body": row.content, "version": row.version}
                              for row in db.scalars(select(ChapterVersion).where(
                                  ChapterVersion.chapter_id == item.id
                              ).order_by(ChapterVersion.version))
                          ]} for item in chapters],
        })
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


async def run_benchmark(settings, engine):
    if not re.fullmatch(r"lisa_test_[0-9a-f]{32}", settings.db_name):
        raise ValueError("RAG fixtures require a disposable lisa_test database")
    gold_bytes = GOLD.read_bytes()
    gold = json.loads(gold_bytes)
    configured = settings.model_copy(update={
        "embedding_model": "deterministic-rag-v1", "embedding_dimensions": 16,
        "embedding_base_url": "https://synthetic.invalid/v1",
        "embedding_api_key": SecretStr("unused-synthetic"),
        "embedding_input_per_million": Decimal("0"),
    })
    prefix = uuid4().hex[:5]
    fixtures = []
    with Session(engine) as db, db.begin():
        for case in gold["cases"]:
            fixtures.append(_seed(db, case, prefix))
    with Session(engine) as db:
        source_snapshot = _source_snapshot(db, fixtures)
    await _index_all(configured, engine)
    rows = []
    for case, seeded in zip(gold["cases"], fixtures, strict=True):
        if case["scenario"] == "revision":
            with Session(engine) as db, db.begin():
                if case["purpose"] == "role":
                    db.get(Character, seeded["character"]).revision += 1
                else:
                    for key in seeded["expected"]:
                        db.get(Memory, UUID(key.split(":")[1])).revision += 1
        context = await retrieve_context(
            configured, engine, seeded["task"], seeded["user"], seeded["project"],
            case["purpose"], case["question"], embed=synthetic_embedding,
            character_id=seeded["character"] if case["purpose"] == "role" else None,
        )
        candidates = context.metrics.get("candidate_keys", [])[:5]
        context = _pack_final(configured, context, case["purpose"], case["scenario"],
                              case["question"])
        finalize_context(engine, context)
        final = context.metrics.get("final_complete_keys", [])
        expected = set(seeded["expected"])
        recall = len(expected.intersection(candidates)) / len(expected) if expected else None
        precision = len(expected.intersection(candidates)) / len(candidates) if candidates else None
        coverage = len(expected.intersection(final)) / len(expected) if expected else None
        safe = not any(identifier in key for identifier in seeded["hidden"]
                       for key in [*candidates, *final])
        safe = safe and not any(text in context.text for text in seeded["forbidden_text"])
        expected_coverage = case["expected_final_coverage"]
        passed = safe and (expected_coverage is None or coverage == expected_coverage)
        rows.append({"id": case["id"], "purpose": case["purpose"],
                     "scenario": case["scenario"], "expected_keys": sorted(expected),
                     "returned_keys": candidates, "final_keys": final, "recall_at_k": recall,
                     "precision_at_k": precision if expected else None,
                     "context_coverage": coverage, "passed": passed,
                     "scope_safe": safe, "omitted": context.metrics.get("omitted", {}),
                     "candidate_count": context.metrics.get("candidates", 0),
                     "deduped_count": context.metrics.get("deduped", 0)})
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                           text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "configuration": {"dataset_version": gold["version"], "k": 5,
                          "dataset_sha256": hashlib.sha256(gold_bytes).hexdigest(),
                          "source_snapshot": source_snapshot,
                          "working_tree_sha256": _code_fingerprint(),
                          "embedding_space": embedding_space(configured),
                          "embedding_model": configured.embedding_model,
                          "max_chars": "1500; capacity cases 300", "code_revision": revision,
                          "evaluation_kind": "deterministic_pipeline",
                          "human_semantic_review": "pending"},
        "results": {**_summary(rows), "dataset_version": gold["version"],
                    "evaluation_kind": "deterministic_pipeline", "cases": rows,
                    "by_purpose": [{"purpose": purpose, **_summary([
                        row for row in rows if row["purpose"] == purpose
                    ])} for purpose in ["chat", "writing", "role"]]},
    }
    with Session(engine) as db, db.begin():
        db.add(RagEvaluationRun(configuration=report["configuration"], results=report["results"]))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runtime/reports/rag-dashboard-baseline.json")
    parser.add_argument("--publish", action="store_true",
                        help="After isolated evaluation, store only its safe report in current DB")
    args = parser.parse_args()
    import psycopg
    from dotenv import dotenv_values
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    env = {**{k: v for k, v in dotenv_values(ROOT / "runtime/.env").items() if v}, **os.environ}
    original_database = env.get("LISA_DB_NAME", "lisa")
    database = "lisa_test_" + uuid4().hex
    admin_info = make_conninfo(host=env["LISA_DB_HOST"], port=env["LISA_DB_PORT"],
                               dbname="postgres", user="postgres",
                               password=env["LISA_DB_ADMIN_PASSWORD"], connect_timeout=5)
    child_env = {**env, "LISA_DB_NAME": database, "LISA_MODEL_API_KEY": "",
                 "LISA_IMAGE_API_KEY": "", "LISA_VISION_API_KEY": "",
                 "LISA_EMBEDDING_API_KEY": "", "PYTHONUTF8": "1"}
    with psycopg.connect(admin_info, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier(env["LISA_DB_USER"])))
        engine = None
        try:
            with psycopg.connect(make_conninfo(admin_info, dbname=database),
                                  autocommit=True) as created:
                created.execute("CREATE EXTENSION IF NOT EXISTS vector")
            migrated = subprocess.run([sys.executable, "-m", "lisa", "migrate"], cwd=ROOT,
                                      env=child_env, capture_output=True, text=True)
            if migrated.returncode:
                raise RuntimeError("Isolated RAG migration failed; no production changes made")
            os.environ.update(child_env)
            settings = Settings()
            engine = make_engine(settings)
            report = asyncio.run(run_benchmark(settings, engine))
        finally:
            if engine:
                engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database)))
            os.environ.update(env)
            os.environ["LISA_DB_NAME"] = original_database
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.publish:
        engine = make_engine(Settings())
        try:
            with Session(engine) as db, db.begin():
                db.add(RagEvaluationRun(configuration=report["configuration"],
                                        results=report["results"]))
        finally:
            engine.dispose()
    print(json.dumps({"output": str(output), **_summary(report["results"]["cases"])}))
    if report["results"]["passed_count"] != report["results"]["sample_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
