import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_author import pending_task
from test_chat import ingest
from test_works import HEADERS, RESULT, document
from test_works import owner as owner
from test_works import project as project

from lisa import recall as recall_module
from lisa.author import execute_author
from lisa.chat import execute_chat
from lisa.model_calls import ModelCallUnknown
from lisa.models import (
    Account,
    Chapter,
    LifeShare,
    Memory,
    MemoryUse,
    Project,
    QQBinding,
    RetrievalReference,
    Task,
)
from lisa.retrieval import ensure_index_tasks, execute_index


@pytest.fixture
def recall_settings(settings):
    return settings.model_copy(
        update={
            "embedding_model": "synthetic-recall-model",
            "embedding_dimensions": 2,
            "embedding_input_per_million": Decimal("1"),
            "model_api_key": SecretStr("synthetic-unused-key"),
        }
    )


def memory(db, user_id, **values):
    item = Memory(
        user_id=user_id, kind="fact", content="用户把蓝色门牌留在旧桥。", allow_writing=True
    )
    for key, value in values.items():
        setattr(item, key, value)
    db.add(item)
    db.commit()
    return item


async def build_index(settings, engine, db):
    tasks = ensure_index_tasks(db, settings)
    ids = [task.id for task in tasks]
    db.commit()

    async def embed(settings, engine, task_id, user_id, texts):
        return [[1.0, 0.0] if "蓝色门牌" in value else [0.0, 1.0] for value in texts]

    for task_id in ids:
        await execute_index(settings, engine, task_id, embed=embed)
    db.expire_all()
    assert all(db.get(Task, task_id).status == "completed" for task_id in ids)


def queue_chat(db, user_id):
    return ingest(
        db, "recall-current", qq_id=db.get(QQBinding, user_id).qq_id, content="还记得蓝色门牌吗？"
    )


def dialogue(reply="我记得这件事。", **values):
    return {"reply": reply, "opinions": [], "commission": None, **values}


def reference_rows(db, task_id):
    db.expire_all()
    return list(db.scalars(select(RetrievalReference).where(RetrievalReference.task_id == task_id)))


@pytest.mark.parametrize("sources", [["chapter"], []])
@pytest.mark.asyncio
async def test_author_uses_saved_retrieval_config_and_exposes_only_prepared_references(
    recall_settings, engine, db, client, owner, project, monkeypatch, sources
):
    work_id = UUID(project["id"])
    earlier = Chapter(
        project_id=work_id,
        title="门牌",
        position=1,
        content=document("桥下藏有蓝色门牌。"),
        version=1,
    )
    latest = Chapter(
        project_id=work_id,
        title="当前场景",
        position=2,
        content=document("她走进新城。"),
        version=1,
    )
    db.add_all([earlier, latest])
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    endpoint = f"/api/projects/{work_id}/retrieval"
    assert (
        client.put(
            endpoint, json={"sources": sources, "limit": 1, "max_chars": 500}, headers=HEADERS
        ).status_code
        == 200
    )
    task_id = pending_task(db, owner, project)
    query = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(recall_module, "generate_embeddings", query)

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert current_task == task_id
        payload = json.loads(messages[0]["content"])
        # These controls do not remove the task's basic saved materials or current scene.
        assert payload["worldbook"] == db.get(Project, work_id).worldbook
        assert payload["context"] == "她走进新城。"
        if sources:
            assert "桥下藏有蓝色门牌。" in instructions
            assert "用户把蓝色门牌" not in instructions
        else:
            assert "检索资料" not in instructions
        return RESULT

    await execute_author(recall_settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed" and task.adopted_at is not None
    assert "蓝色门牌" not in json.dumps(task.input_snapshot, ensure_ascii=False)
    refs = client.get(f"/api/tasks/{task_id}/references").json()["references"]
    if sources:
        query.assert_awaited_once()
        assert query.await_args.args[2] == task_id
        assert len(refs) == 1 and refs[0]["text"] == "桥下藏有蓝色门牌。"
        assert refs[0]["state"] == "available" and refs[0]["source_kind"] == "chapter"
    else:
        query.assert_not_awaited()
        assert refs == []


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.asyncio
async def test_no_ready_index_skips_query_embedding(
    recall_settings,
    engine,
    db,
    owner,
    project,
    monkeypatch,
    consumer,
):
    query = AsyncMock(side_effect=AssertionError("No ready index should require no query"))
    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    generate = AsyncMock(return_value=dialogue() if consumer == "chat" else RESULT)
    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id, generate=generate)
    query.assert_not_awaited()
    generate.assert_awaited_once()
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert reference_rows(db, task_id) == []


@pytest.mark.asyncio
async def test_chat_recall_records_actual_memory_and_only_supplied_life_mentions(
    recall_settings,
    engine,
    db,
    owner,
    project,
    monkeypatch,
):
    item = memory(
        db, owner, kind="life", life_kind="event", occurred_at=datetime(2026, 1, 2, tzinfo=UTC)
    )
    hidden = memory(
        db,
        owner,
        kind="life",
        content="尚未分享的另一个事件",
        life_kind="event",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        allow_chat=False,
    )
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner)
    queries, generations = [], []

    async def query(settings, engine, current_task, user_id, texts):
        queries.append((current_task, user_id, texts))
        with Session(engine) as check:
            assert check.get(Task, current_task).status == "running"
        return [[1.0, 0.0]]

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        generations.append(current_task)
        assert item.content in instructions and str(item.id) in instructions
        assert hidden.content not in instructions and str(hidden.id) not in instructions
        return dialogue(
            "我记得蓝色门牌。",
            life_mentions=[
                {"memory_id": str(item.id), "evidence": "蓝色门牌"},
                {"memory_id": str(hidden.id), "evidence": "蓝色门牌"},
                {"memory_id": str(item.id), "evidence": "回复没有的原文"},
            ],
        )

    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    await execute_chat(recall_settings, engine, task_id, generate=generate)
    assert queries == [(task_id, owner, ["还记得蓝色门牌吗？"])]
    assert generations == [task_id]
    refs = reference_rows(db, task_id)
    assert [(row.source_kind, row.source_id) for row in refs] == [("memory", item.id)]
    assert db.get(MemoryUse, (item.id, task_id)) is not None
    assert db.get(MemoryUse, (hidden.id, task_id)) is None
    assert list(db.scalars(select(LifeShare.memory_id).where(LifeShare.task_id == task_id))) == [
        item.id
    ]
    assert db.get(Memory, item.id).shared_at is None


@pytest.mark.asyncio
async def test_chat_recall_excludes_project_and_chapter_sources(
    recall_settings,
    engine,
    db,
    owner,
    project,
    monkeypatch,
):
    work = db.get(Project, UUID(project["id"]))
    work.constraints = "仅作者可见的作品约束：蓝色门牌必须沉默。"
    db.add(
        Chapter(
            project_id=work.id,
            title="不可带入聊天的章节",
            position=1,
            content=document("仅作者可见的章节正文：蓝色门牌在桥下。"),
            version=1,
        )
    )
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner)
    monkeypatch.setattr(
        recall_module, "generate_embeddings", AsyncMock(return_value=[[1.0, 0.0]])
    )

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert item.content in instructions
        assert "仅作者可见的作品约束" not in instructions
        assert "仅作者可见的章节正文" not in instructions
        return dialogue()

    await execute_chat(recall_settings, engine, task_id, generate=generate)
    assert [(row.source_kind, row.source_id) for row in reference_rows(db, task_id)] == [
        ("memory", item.id)
    ]


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.asyncio
async def test_unknown_query_stops_before_generation(
    recall_settings,
    engine,
    db,
    owner,
    project,
    monkeypatch,
    consumer,
):
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    query = AsyncMock(side_effect=ModelCallUnknown("synthetic query outcome unknown"))
    generate = AsyncMock(side_effect=AssertionError("Unknown query must stop generation"))
    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id, generate=generate)
    query.assert_awaited_once()
    generate.assert_not_awaited()
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"
    assert db.get(Task, task_id).error == "model_unknown"
    assert reference_rows(db, task_id) == []


@pytest.mark.parametrize("consumer, change", [("chat", "delete"), ("author", "correct")])
@pytest.mark.asyncio
async def test_memory_change_during_query_stops_before_generation(
    recall_settings,
    engine,
    client,
    db,
    owner,
    project,
    monkeypatch,
    consumer,
    change,
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    before_revision = db.get(Account, owner).memory_revision

    async def query(*args):
        if change == "delete":
            response = client.delete(f"/api/memories/{item.id}", headers=HEADERS)
            assert response.status_code == 204
        else:
            response = client.put(
                f"/api/memories/{item.id}",
                headers=HEADERS,
                json={
                    "content": "门牌是红色的。",
                    "kind": "fact",
                    "allow_writing": True,
                },
            )
            assert response.status_code == 200
        return [[1.0, 0.0]]

    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    generate = AsyncMock(side_effect=AssertionError("Changed context must stop generation"))
    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id, generate=generate)
    generate.assert_not_awaited()
    db.expire_all()
    assert db.get(Account, owner).memory_revision == before_revision + 1
    assert db.get(Task, task_id).error == "context_changed"
    assert reference_rows(db, task_id) == []


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.asyncio
async def test_oversized_optional_recall_dropped_without_reference_records(
    recall_settings,
    engine,
    db,
    owner,
    project,
    monkeypatch,
    consumer,
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    monkeypatch.setattr(recall_module, "generate_embeddings", AsyncMock(return_value=[[1.0, 0.0]]))
    original = recall_module.recall

    def oversized(*args, **kwargs):
        context = original(*args, **kwargs)
        assert context.memory_ids == [item.id]
        context.text += "检索上下文过长" * 6000
        return context

    monkeypatch.setattr(recall_module, "recall", oversized)

    async def generate(settings, engine, task_id, user_id, instructions, messages):
        assert item.content not in instructions and "检索上下文过长" not in instructions
        return dialogue() if consumer == "chat" else RESULT

    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id, generate=generate)
    assert reference_rows(db, task_id) == []
    assert db.get(MemoryUse, (item.id, task_id)) is None
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_author_cancellation_during_query_never_generates_or_adopts(
    recall_settings,
    engine,
    client,
    db,
    owner,
    project,
    monkeypatch,
):
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = pending_task(db, owner, project)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def query(*args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return [[1.0, 0.0]]

    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    generate = AsyncMock(side_effect=AssertionError("Cancelled query must not generate"))
    running = asyncio.create_task(
        execute_author(recall_settings, engine, task_id, generate=generate)
    )
    await asyncio.wait_for(started.wait(), 2)
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
    await asyncio.wait_for(running, 2)
    assert cancelled.is_set()
    generate.assert_not_awaited()
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(Task, task_id).adopted_at is None
    assert reference_rows(db, task_id) == []
