import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from lisa.models import Account, Memory, ModelCall, RetrievalObservation, Task
from lisa.observability import context_keys, finalize_context
from lisa.recall import retrieve_context
from lisa.retrieval import Context, Source, _assemble_context
from scripts.benchmark_rag import run_benchmark


def test_worldbook_middle_slice_has_same_identity_as_complete_entry():
    source_id, owner = uuid4(), uuid4()
    entry_id = uuid4().hex
    body = "完整条目的前半段。后半段含需要的信息。"
    source = Source("project", source_id, owner, 3, body,
                    {"worldbook_entries": [{"id": entry_id, "title": "合成条目",
                                            "related_ids": []}]},
                    [("worldbook_entry:" + entry_id, 0, len(body))])
    context = _assemble_context([
        (source, 5, len(body), 1, "synthetic"),
        (source, 10, len(body), 2, "synthetic"),
    ], 1500, 5)
    expected = f"project:{source_id}:worldbook_entry:{entry_id}"
    assert context.metrics["candidates"] == 2
    assert context.metrics["deduped"] == 1
    assert context.metrics["candidate_keys"] == [expected]
    assert context_keys(context) == [expected]
    assert context.metrics["assembled"] == 1


@pytest.mark.asyncio
async def test_retrieval_observation_has_no_content_and_tracks_final_drop(settings, engine, db):
    user = Account(username="rag_private", password_hash="unused-synthetic")
    db.add(user)
    db.flush()
    item = Memory(user_id=user.id, kind="fact", content="PRIVATE_SOURCE_SENTINEL",
                  allow_chat=True)
    task = Task(user_id=user.id, kind="chat", status="running")
    db.add_all([item, task])
    db.commit()
    context = await retrieve_context(settings, engine, task.id, user.id, None, "chat",
                                     "PRIVATE_QUERY_SENTINEL")
    assert context.text
    assert context.observation_id
    context = context.discarded("request_budget")
    finalize_context(engine, context)
    db.expire_all()
    row = db.get(RetrievalObservation, context.observation_id)
    encoded = json.dumps(row.metrics)
    assert "PRIVATE_SOURCE_SENTINEL" not in encoded
    assert "PRIVATE_QUERY_SENTINEL" not in encoded
    assert row.metrics["final_count"] == 0
    assert row.metrics["final_chars"] == 0
    assert row.metrics["omitted"]["request_budget"] == 1
    assert row.metrics["route"] == "recent"
    db.delete(user)
    db.commit()
    assert db.scalar(select(RetrievalObservation.id)) is None


@pytest.mark.asyncio
async def test_observation_failure_does_not_break_context(settings, engine, db, monkeypatch):
    from lisa import observability

    user = Account(username="rag_capture_failure", password_hash="unused-synthetic")
    db.add(user)
    db.flush()
    db.add(Memory(user_id=user.id, kind="fact", content="合成记忆。"))
    task = Task(user_id=user.id, kind="chat", status="running")
    db.add(task)
    db.commit()

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(observability, "Session", unavailable)
    context = await retrieve_context(settings, engine, task.id, user.id, None, "chat", "问题")
    assert context.text
    assert context.observation_id is None
    finalize_context(engine, Context())


@pytest.mark.asyncio
async def test_deterministic_baseline_exercises_production_retrieval(settings, engine, db):
    report = await run_benchmark(settings, engine)
    assert report["results"]["sample_count"] == 40
    assert report["configuration"]["evaluation_kind"] == "deterministic_pipeline"
    assert report["configuration"]["human_semantic_review"] == "pending"
    assert all(row["scope_safe"] for row in report["results"]["cases"])
    assert report["results"]["passed_count"] == 40, [
        row for row in report["results"]["cases"] if not row["passed"]
    ]
    assert db.scalar(select(ModelCall.id)) is None  # Deterministic vectors made no provider calls.
    assert db.scalar(select(RetrievalObservation.id)) is not None
    assert len(report["results"]["by_purpose"]) == 3
    assert report["configuration"]["source_snapshot"] != report["configuration"]["dataset_sha256"]
    assert len(report["configuration"]["working_tree_sha256"]) == 64
    worldbook = next(row for row in report["results"]["cases"]
                     if row["scenario"] == "worldbook_middle")
    assert worldbook["candidate_count"] > worldbook["deduped_count"]
    for row in report["results"]["cases"]:
        if row["scenario"] == "long_budget":
            assert row["recall_at_k"] == 1
            assert row["context_coverage"] == 0


@pytest.mark.asyncio
async def test_benchmark_refuses_live_database(settings, engine):
    configured = settings.model_copy(update={"db_name": "lisa"})
    with pytest.raises(ValueError, match="disposable"):
        await run_benchmark(configured, engine)


def test_whole_memory_budget_omission_keeps_candidate_metric():
    source = Source("memory", uuid4(), uuid4(), 1, "合成完整记忆" * 220,
                    {"type": "fact"}, [("memory", 0, 1320)])
    context = _assemble_context([(source, 0, 700, 0, "test")], 300, 5)
    assert context.metrics["deduped"] == 1
    assert context.metrics["assembled"] == 0
    assert context.metrics["omitted"] == {"character_budget": 1}
    assert context.text == ""


def test_source_field_dedup_does_not_change_existing_fragment_assembly():
    source = Source("chapter", uuid4(), uuid4(), 1, "章节正文。" * 100,
                    {}, [("content", 0, 600)])
    context = _assemble_context([(source, 0, 100, 0, "test"),
                                 (source, 100, 200, 1, "test")], 1500, 5)
    assert len(context.references) == 2
    assert context.metrics["deduped"] == 1
    assert context.metrics["assembled"] == 1
    assert len(context_keys(context)) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["chat", "writing"])
async def test_empty_library_is_no_sources_instead_of_empty_fallback(
    settings, engine, db, purpose
):
    from lisa.models import Project

    user = Account(username="rag_empty_library", password_hash="unused-synthetic")
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, title="空资料作品",
                      retrieval_config={"sources": ["memory"]})
    db.add(project)
    db.flush()
    task = Task(user_id=user.id, project_id=project.id, kind="chat", status="running")
    db.add(task)
    db.commit()
    context = await retrieve_context(settings, engine, task.id, user.id, project.id,
                                     purpose, "没有对应的资料")
    assert context.text == ""
    assert context.metrics["state"] == "no_sources"
    assert context.metrics["route"] == "none"
    assert context.metrics["degraded"] is False
    assert context.metrics["candidates"] == 0
    assert context.metrics["assembled"] == 0
    project.retrieval_config = {"sources": []}
    db.commit()
    if purpose == "writing":
        disabled = await retrieve_context(settings, engine, task.id, user.id, project.id,
                                          purpose, "来源明确关闭")
        assert disabled.metrics["state"] == "disabled"
