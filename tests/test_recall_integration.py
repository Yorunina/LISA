import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError
from procrastinate.exceptions import JobRetry
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_author import pending_task
from test_chat import ingest
from test_embeddings import response as embedding_response
from test_embeddings import sdk as sdk
from test_shared import create as create_shared
from test_works import HEADERS, RESULT, document
from test_works import owner as owner
from test_works import project as project

from lisa import model_calls
from lisa import recall as recall_module
from lisa.author import WritingResult, execute_author
from lisa.chat import execute_chat
from lisa.dialogue import Dialogue
from lisa.model_calls import ModelBusy, ModelCallFailed, ModelCallUnknown, generate_reply
from lisa.models import (
    Account,
    BudgetDay,
    Chapter,
    LifeShare,
    Memory,
    MemoryUse,
    ModelCall,
    OutboundMessage,
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


def fail_embedding(sdk, failure):
    async def provider(**kwargs):
        if failure in {"connect", "read_timeout"}:
            cause = httpx2.ConnectError if failure == "connect" else httpx2.ReadTimeout
            raise APIConnectionError(
                request=httpx2.Request("POST", "https://embedding.invalid")
            ) from cause("synthetic")
        if failure in {"invalid", "invalid_no_usage"}:
            return embedding_response(data=[], tokens=None if failure == "invalid_no_usage" else 20)
        raise APIStatusError(
            "synthetic", response=httpx2.Response(
                int(failure), request=httpx2.Request("POST", "https://embedding.invalid")
            ), body=None,
        )

    sdk.embeddings.side_effect = provider


@pytest.mark.parametrize("consumer,change", [
    ("chat", None), ("author", None), ("chat", "memory"),
    ("author", "memory"), ("author", "sources"),
])
@pytest.mark.asyncio
async def test_busy_retry_after_query_fallback_does_not_repeat_failed_embedding(
    recall_settings, engine, db, client, owner, project, sdk, consumer, change
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    fail_embedding(sdk, "invalid")
    attempts = []

    async def generate(settings, engine, task_id, user_id, instructions, messages):
        attempts.append(instructions)
        assert (item.content in instructions) == (not (change == "sources" and len(attempts) > 1))
        if len(attempts) == 1:
            raise ModelBusy("synthetic occupied slot")
        await generate_reply(settings, engine, task_id, user_id, instructions, messages)
        return dialogue() if consumer == "chat" else RESULT

    execute = execute_chat if consumer == "chat" else execute_author
    with pytest.raises(JobRetry):
        await execute(recall_settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "pending"
    if change == "memory":
        assert client.put(
            f"/api/memories/{item.id}", headers=HEADERS,
            json={"content": "繁忙等待期间保存的新约定。", "kind": "fact", "allow_writing": True},
        ).status_code == 200
        db.expire_all()
    elif change == "sources":
        assert client.put(
            f"/api/projects/{project['id']}/retrieval", headers=HEADERS, json={"sources": []},
        ).status_code == 200
    await execute(recall_settings, engine, task_id, generate=generate)
    sdk.embeddings.assert_awaited_once()
    sdk.responses.assert_awaited_once()
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    calls = list(db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)))
    assert len(calls) == 2
    assert next(c for c in calls if c.state == "failed").configuration["query_fallback"] is True


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.parametrize("failure", ["connect", "400", "invalid", "invalid_no_usage"])
@pytest.mark.asyncio
async def test_settled_query_failure_uses_recent_memory_and_same_ledger(
    recall_settings, engine, db, owner, project, sdk, consumer, failure
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    fail_embedding(sdk, failure)
    sdk.responses.return_value = SimpleNamespace(
        status="completed", output_text=(
            Dialogue.model_validate(dialogue()).model_dump_json() if consumer == "chat"
            else WritingResult.model_validate(RESULT).model_dump_json()
        ),
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )
    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert item.content in sdk.responses.call_args.kwargs["instructions"]
    assert "有效记忆" in sdk.responses.call_args.kwargs["instructions"]
    sdk.embeddings.assert_awaited_once()
    sdk.responses.assert_awaited_once()
    calls = list(db.scalars(select(ModelCall).where(ModelCall.task_id == task_id).order_by(
        ModelCall.started_at
    )))
    assert [c.state for c in calls] == ["failed", "succeeded"]
    assert calls[0].configuration["api"] == "embeddings"
    if failure == "invalid":
        assert calls[0].charged_usd == Decimal("0.000020")
    elif failure == "invalid_no_usage":
        assert calls[0].charged_usd == calls[0].reserved_usd
    else:
        assert calls[0].charged_usd == 0
    day = db.get(BudgetDay, calls[0].budget_day)
    assert day.reserved_usd == 0
    assert day.spent_usd == sum(c.charged_usd for c in calls)


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.parametrize("failure", ["502", "read_timeout"])
@pytest.mark.asyncio
async def test_unknown_query_never_falls_back_with_real_accounting(
    recall_settings, engine, db, owner, project, sdk, consumer, failure
):
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    fail_embedding(sdk, failure)
    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id)
    db.expire_all()
    assert db.get(Task, task_id).error == "model_unknown"
    sdk.embeddings.assert_awaited_once()
    sdk.responses.assert_not_awaited()
    call = db.scalar(select(ModelCall).where(ModelCall.task_id == task_id))
    assert call.state == "unknown" and call.charged_usd == call.reserved_usd


@pytest.mark.parametrize("exhausted", ["calls", "money"])
@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.asyncio
async def test_fallback_keeps_billing_and_each_consumers_current_allowance(
    recall_settings, engine, db, owner, project, sdk, monkeypatch, exhausted, consumer
):
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    if exhausted == "calls":
        monkeypatch.setattr(model_calls, "MAX_TASK_CALLS", 1)
        monkeypatch.setattr(model_calls, "MAX_WRITING_CALLS", 1)
    else:
        recall_settings = recall_settings.model_copy(update={"model_task_usd": Decimal("0.01")})
    # A completed but malformed vector response may still be expensive.
    sdk.embeddings.return_value = embedding_response(
        data=[], tokens=20 if exhausted == "calls" else 1_000_000
    )
    sdk.responses.return_value = SimpleNamespace(
        status="completed", output_text=Dialogue(**dialogue()).model_dump_json(),
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    await (execute_chat if consumer == "chat" else execute_author)(
        recall_settings, engine, task_id
    )
    db.expire_all()
    sdk.embeddings.assert_awaited_once()
    calls = list(db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)))
    if consumer == "chat":
        assert db.get(Task, task_id).status == "completed"
        sdk.responses.assert_awaited_once()
        assert sorted(call.state for call in calls) == ["failed", "succeeded"]
        assert all(not call.budget_exempt for call in calls)
    else:
        assert db.get(Task, task_id).error == "budget_exhausted"
        sdk.responses.assert_not_awaited()
        assert len(calls) == 1 and calls[0].state == "failed"
    assert db.get(BudgetDay, calls[0].budget_day).reserved_usd == 0


@pytest.mark.parametrize("change", ["memory", "cancel", "disabled", "sources"])
@pytest.mark.asyncio
async def test_definitive_failure_rechecks_live_state_before_fallback(
    recall_settings, engine, db, client, owner, project, sdk, change
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = pending_task(db, owner, project)

    async def reject(**kwargs):
        if change == "memory":
            assert client.delete(f"/api/memories/{item.id}", headers=HEADERS).status_code == 204
        elif change == "sources":
            assert client.put(
                f"/api/projects/{project['id']}/retrieval", json={"sources": []}, headers=HEADERS
            ).status_code == 200
        else:
            with Session(engine) as current, current.begin():
                if change == "cancel":
                    current.get(Task, task_id).status = "cancelled"
                else:
                    current.get(Account, owner).active = False
        return embedding_response(data=[])

    sdk.embeddings.side_effect = reject
    sdk.responses.return_value = SimpleNamespace(
        status="completed", output_text=WritingResult.model_validate(RESULT).model_dump_json(),
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )
    await execute_author(recall_settings, engine, task_id)
    db.expire_all()
    if change == "sources":
        assert item.content not in sdk.responses.call_args.kwargs["instructions"]
        assert reference_rows(db, task_id) == []
        assert db.get(Task, task_id).status == "completed"
    else:
        sdk.responses.assert_not_awaited()
        assert db.get(Task, task_id).adopted_at is None
        if change == "memory":
            assert db.get(Task, task_id).error == "context_changed"


@pytest.mark.asyncio
async def test_author_does_not_query_embeddings_for_public_only_index(
    recall_settings, engine, db, client, owner, project, monkeypatch
):
    create_shared(client)
    await build_index(recall_settings, engine, db)
    memory(db, owner)  # No private index, but current recent fallback remains usable.
    db.get(Project, UUID(project["id"])).retrieval_config = {"sources": ["memory"]}
    db.commit()
    task_id = pending_task(db, owner, project)
    query = AsyncMock(side_effect=AssertionError("Author cannot use public memes"))
    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    await execute_author(recall_settings, engine, task_id, generate=AsyncMock(return_value=RESULT))
    query.assert_not_awaited()
    assert all(r.source_kind != "shared" for r in reference_rows(db, task_id))


@pytest.mark.parametrize("failure", [ModelCallFailed("generic"), ValueError("program error")])
@pytest.mark.asyncio
async def test_query_does_not_swallow_unclassified_errors(
    recall_settings, engine, db, owner, project, failure
):
    memory(db, owner)
    await build_index(recall_settings, engine, db)
    with pytest.raises(type(failure)):
        await recall_module.retrieve_context(
            recall_settings, engine, queue_chat(db, owner), owner, UUID(project["id"]),
            "chat", "查询", embed=AsyncMock(side_effect=failure),
        )


@pytest.mark.parametrize("mode", ["text_only", "public_only", "both", "private_only"])
@pytest.mark.asyncio
async def test_public_results_do_not_replace_private_memory_selection(
    recall_settings, engine, db, client, owner, project, monkeypatch, mode
):
    public = create_shared(client) if mode != "private_only" else None
    semantic = memory(db, owner, content="蓝色门牌：只有周末才能借用。") if mode in {
        "both", "private_only"
    } else None
    if mode != "text_only":
        await build_index(recall_settings, engine, db)
    recent = memory(db, owner, content="近期约定：不要在夜间打电话。")
    query = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    task_id = queue_chat(db, owner)
    selected = semantic or recent

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert selected.content in instructions
        if semantic:
            assert recent.content not in instructions
        if public:
            assert public["title"] in instructions
        return dialogue()

    await execute_chat(recall_settings, engine, task_id, generate=generate)
    assert query.await_count == (0 if mode == "text_only" else 1)
    refs = reference_rows(db, task_id)
    assert db.get(Task, task_id).status == "completed"
    assert db.get(MemoryUse, (selected.id, task_id)) is not None
    if semantic:
        assert db.get(MemoryUse, (recent.id, task_id)) is None
        assert [r.source_id for r in refs if r.source_kind == "memory"] == [semantic.id]
    assert [r.source_id for r in refs if r.source_kind == "shared"] == (
        [UUID(public["id"])] if public else []
    )


@pytest.mark.parametrize("sources", [[], ["chapter"], ["memory"]])
@pytest.mark.asyncio
async def test_author_empty_recall_fallback_respects_sources_and_records_current_reference(
    recall_settings, engine, db, client, owner, project, monkeypatch, sources
):
    item = memory(db, owner, content="只在冬日使用的创作约定。", basis="用户明确填写。")
    work = db.get(Project, UUID(project["id"]))
    work.retrieval_config = {"sources": sources, "limit": 1, "max_chars": 300}
    db.commit()
    task_id = pending_task(db, owner, project)
    query = AsyncMock(side_effect=AssertionError("No index requires no embedding"))
    monkeypatch.setattr(recall_module, "generate_embeddings", query)

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert (item.content in instructions) == ("memory" in sources)
        assert (item.basis in instructions) == ("memory" in sources)
        return RESULT

    await execute_author(recall_settings, engine, task_id, generate=generate)
    query.assert_not_awaited()
    refs = client.get(f"/api/tasks/{task_id}/references").json()["references"]
    if "memory" in sources:
        assert len(refs) == 1 and refs[0]["state"] == "available"
        assert refs[0]["text"] == item.content + "\n推测依据：" + item.basis
    else:
        assert refs == []
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert item.content not in json.dumps(db.get(Task, task_id).input_snapshot, ensure_ascii=False)


@pytest.mark.parametrize("limit,max_chars,count", [(1, 1200, 1), (5, 300, 1), (5, 1200, 3)])
@pytest.mark.asyncio
async def test_author_recent_memories_obey_saved_count_and_character_limits(
    recall_settings, engine, db, owner, project, monkeypatch, limit, max_chars, count
):
    items = [memory(db, owner, content=f"约定{i}：" + "完整" * 40) for i in range(3)]
    oversized = memory(db, owner, content="超长内容" * 500)
    work = db.get(Project, UUID(project["id"]))
    work.retrieval_config = {"sources": ["memory"], "limit": limit, "max_chars": max_chars}
    db.commit()
    task_id = pending_task(db, owner, project)
    monkeypatch.setattr(
        recall_module, "generate_embeddings", AsyncMock(side_effect=AssertionError("No query"))
    )
    provided = []

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert "超长内容" not in instructions
        provided.extend(item.id for item in items if item.content in instructions)
        assert len(provided) == count
        recent = instructions[instructions.index("\n有效记忆"):]
        assert len(recent) <= min(1200, max_chars)
        for line in recent.splitlines()[2:]:
            assert json.loads(line)["content"] in [item.content for item in items]
        return RESULT

    await execute_author(recall_settings, engine, task_id, generate=generate)
    refs = reference_rows(db, task_id)
    assert {r.source_id for r in refs} == set(provided)
    assert oversized.id not in provided
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_author_does_not_restore_fallback_after_sources_change_during_query(
    recall_settings, engine, db, client, owner, project, monkeypatch
):
    item = memory(db, owner)
    await build_index(recall_settings, engine, db)
    task_id = pending_task(db, owner, project)

    async def query(*args):
        response = client.put(
            f"/api/projects/{project['id']}/retrieval", json={"sources": []}, headers=HEADERS
        )
        assert response.status_code == 200
        return [[1.0, 0.0]]

    monkeypatch.setattr(recall_module, "generate_embeddings", query)

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert item.content not in instructions and "有效记忆" not in instructions
        return RESULT

    await execute_author(recall_settings, engine, task_id, generate=generate)
    assert reference_rows(db, task_id) == []
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.parametrize("consumer", ["chat", "author"])
@pytest.mark.parametrize("with_short", [True, False])
@pytest.mark.asyncio
async def test_overlong_semantic_memories_allow_only_complete_recent_fallback(
    recall_settings, engine, db, owner, project, monkeypatch, consumer, with_short
):
    memory(db, owner, content="蓝色门牌的背景" * 220 + "现在不再适用。")
    await build_index(recall_settings, engine, db)
    short = memory(db, owner, content="短约定：只有收到确认才出发。") if with_short else None
    task_id = queue_chat(db, owner) if consumer == "chat" else pending_task(db, owner, project)
    query = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(recall_module, "generate_embeddings", query)

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert "蓝色门牌的背景" not in instructions
        if short:
            assert short.content in instructions
        else:
            assert "有效记忆" not in instructions and "检索资料" not in instructions
        return dialogue() if consumer == "chat" else RESULT

    execute = execute_chat if consumer == "chat" else execute_author
    await execute(recall_settings, engine, task_id, generate=generate)
    query.assert_awaited_once()
    refs = reference_rows(db, task_id)
    assert [r.source_id for r in refs] == ([short.id] if short and consumer == "author" else [])
    if short and consumer == "chat":
        assert db.get(MemoryUse, (short.id, task_id)) is not None
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("private_oversized", [False, True])
@pytest.mark.asyncio
async def test_total_budget_drops_public_first_then_uses_existing_fallback(
    recall_settings, engine, db, client, owner, project, monkeypatch, semantic, private_oversized
):
    item = memory(db, owner)
    public = create_shared(client)
    if semantic:
        await build_index(recall_settings, engine, db)
    original = recall_module.recall_shared

    def oversized(*args, **kwargs):
        context = original(*args, **kwargs)
        assert public["title"] in context.text
        context.text += "公共资料过长" * 20000
        return context

    monkeypatch.setattr(recall_module, "recall_shared", oversized)
    if private_oversized:
        original_private = recall_module.recall if semantic else recall_module.memory_context

        def large_private(*args, **kwargs):
            result = original_private(*args, **kwargs)
            if semantic:
                result.text += "私人资料过长" * 20000
                return result
            return result + "私人资料过长" * 20000

        monkeypatch.setattr(
            recall_module, "recall" if semantic else "memory_context", large_private
        )
    query = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(recall_module, "generate_embeddings", query)
    task_id = queue_chat(db, owner)

    async def generate(settings, engine, current_task, user_id, instructions, messages):
        assert (item.content in instructions) == (not private_oversized)
        assert public["title"] not in instructions and "公共资料过长" not in instructions
        assert "私人资料过长" not in instructions
        return dialogue(meme_id=public["id"])

    await execute_chat(recall_settings, engine, task_id, generate=generate)
    assert query.await_count == int(semantic)
    refs = reference_rows(db, task_id)
    assert [r.source_id for r in refs] == ([item.id] if semantic and not private_oversized else [])
    assert (db.get(MemoryUse, (item.id, task_id)) is not None) == (not private_oversized)
    assert all(row.meme_id is None for row in db.scalars(
        select(OutboundMessage).where(OutboundMessage.task_id == task_id)
    ))
    assert db.get(Task, task_id).status == "completed"


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
            assert "有效记忆" not in instructions
            assert "用户把蓝色门牌" not in instructions
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
