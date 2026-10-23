"""Memory controls must reach the actual chat and writing call boundaries."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_author import pending_task
from test_works import HEADERS, RESULT
from test_works import owner as owner
from test_works import project as project

from lisa.author import WritingResult, _snapshot, execute_author
from lisa.chat import execute_chat, ingest_message
from lisa.dialogue import Conversation, Dialogue
from lisa.memory import MemoryCandidate
from lisa.model_calls import MAX_INPUT_BYTES, _reply_request, _request_size
from lisa.models import ChatMessage, Memory, OutboundMessage, Project, Task


def test_memory_output_schema_is_strict_even_with_local_defaults(settings):
    schema = _reply_request(settings, "", [], Dialogue)["text"]["format"]["schema"]

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            assert "default" not in node
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    check(schema)


def message(db, number, content):
    task_id = ingest_message(
        db,
        bot_id="memory-test",
        message_id=str(number),
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content=content,
    )
    db.commit()
    return task_id


def remember(client, content, **kwargs):
    response = client.post("/api/memories", json={"content": content, **kwargs}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_chat_adopts_grounded_memory_without_a_project(settings, engine, db, owner):
    task_id = message(db, 1, "我喜欢缓慢而具体的回复")

    async def generate(*args):
        return Conversation(
            reply="那我们慢慢聊。",
            memories=[
                MemoryCandidate(
                    content="用户喜欢缓慢而具体的回复。",
                    kind="preference",
                    evidence="我喜欢缓慢而具体的回复",
                    allow_writing=True,
                ),
                MemoryCandidate(content="没有来源的猜测", kind="fact", evidence="不存在的原话"),
            ],
        )

    await execute_chat(settings, engine, task_id, generate=generate)
    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    memories = db.scalars(select(Memory)).all()
    assert len(memories) == 1
    assert memories[0].kind == "preference" and memories[0].project_id is None
    source = db.get(ChatMessage, memories[0].source_message_id)
    assert source.task_id == task_id

    async def followup(*args):
        assert "用户喜欢缓慢而具体的回复。" in args[-2]
        return Conversation(reply="记得，我们继续。")

    await execute_chat(settings, engine, message(db, 2, "记得我的交流偏好吗"), generate=followup)


@pytest.mark.asyncio
async def test_corrected_memory_excludes_raw_source_and_echo(
    settings,
    engine,
    db,
    client,
    owner,
):
    first = message(db, 1, "我住在海边")

    async def original(*args):
        return Conversation(
            reply="你住在海边。",
            memories=[
                MemoryCandidate(
                    content="用户住在海边。",
                    kind="fact",
                    evidence="我住在海边",
                )
            ],
        )

    await execute_chat(settings, engine, first, generate=original)

    async def echo(*args):
        return Conversation(reply="我们刚聊过你在海边的住处。")

    await execute_chat(settings, engine, message(db, 2, "接着刚才的话题"), generate=echo)
    item = client.get("/api/memories").json()[0]
    response = client.put(
        f"/api/memories/{item['id']}",
        json={"content": "用户住在山城。", "kind": "fact"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    async def corrected(*args):
        assert "用户住在山城。" in args[-2]
        assert "海边" not in args[-2]
        assert all("海边" not in item["content"] for item in args[-1])
        return Conversation(reply="山城最近怎么样？")

    await execute_chat(settings, engine, message(db, 3, "聊聊我的住处"), generate=corrected)
    assert (
        client.get(f"/api/memories/{item['id']}/source").json()["message"]["content"]
        == "我住在海边"
    )


@pytest.mark.asyncio
async def test_deleted_memory_is_absent_from_both_history_and_recall(
    settings,
    engine,
    db,
    client,
    owner,
):
    first = message(db, 1, "我养了一只叫小雨的猫")

    async def original(*args):
        return Conversation(
            reply="小雨一定很可爱。",
            memories=[
                MemoryCandidate(
                    content="用户有一只猫叫小雨。",
                    kind="fact",
                    evidence="叫小雨的猫",
                )
            ],
        )

    await execute_chat(settings, engine, first, generate=original)

    async def echo(*args):
        return Conversation(reply="我们刚聊过小雨。")

    await execute_chat(settings, engine, message(db, 2, "接着刚才的话题"), generate=echo)
    item = client.get("/api/memories").json()[0]
    assert client.delete(f"/api/memories/{item['id']}", headers=HEADERS).status_code == 204

    async def next_turn(*args):
        assert "小雨" not in args[-2]
        assert all("小雨" not in item["content"] for item in args[-1])
        return Conversation(reply="今天想聊什么？")

    await execute_chat(settings, engine, message(db, 3, "继续聊"), generate=next_turn)
    assert client.get("/api/memories").json() == []


@pytest.mark.asyncio
async def test_source_deleted_inflight_does_not_reappear_as_reply_or_memory(
    settings,
    engine,
    db,
    client,
    owner,
):
    task_id = message(db, 1, "旧的私人信息")
    source = db.scalar(select(ChatMessage).where(ChatMessage.task_id == task_id))

    async def generate(*args):
        assert client.delete(f"/api/chat-messages/{source.id}", headers=HEADERS).status_code == 204
        return Conversation(
            reply="重复旧的私人信息",
            memories=[
                MemoryCandidate(
                    content="旧的私人信息",
                    kind="fact",
                    evidence="旧的私人信息",
                )
            ],
        )

    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).error == "context_changed"
    assert db.scalar(select(Memory)) is None
    assert db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant")) is None
    outgoing = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert "旧的私人信息" not in outgoing.content


@pytest.mark.asyncio
async def test_cleared_pending_batch_never_calls_model(settings, engine, db, client, owner):
    task_id = message(db, 1, "清理后不再处理")
    source = db.scalar(select(ChatMessage).where(ChatMessage.task_id == task_id))
    client.delete(f"/api/chat-messages/{source.id}", headers=HEADERS)

    async def must_not_call(*args):
        pytest.fail("Deleted input reached model")

    await execute_chat(settings, engine, task_id, generate=must_not_call)
    db.expire_all()
    assert db.get(Task, task_id).error == "empty_input"


@pytest.mark.asyncio
async def test_author_uses_only_allowed_scoped_memories_without_snapshot_copies(
    settings,
    engine,
    db,
    client,
    owner,
    project,
):
    remember(client, "聊天专用的私人经历", allow_writing=False)
    remember(client, "允许创作用的慢节奏偏好", kind="preference", allow_writing=True)
    remember(client, "这部作品的文学参考", project_id=project["id"], allow_writing=True)
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        instructions = args[-2]
        assert "允许创作用的慢节奏偏好" in instructions
        assert "这部作品的文学参考" in instructions
        assert "聊天专用的私人经历" not in instructions
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed"
    assert "允许创作用的慢节奏偏好" not in str(task.input_snapshot)


@pytest.mark.asyncio
async def test_author_discards_late_result_after_memory_restriction(
    settings,
    engine,
    db,
    client,
    owner,
    project,
):
    item = remember(client, "可以用的旧记忆", allow_writing=True)
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        client.put(
            f"/api/memories/{item['id']}",
            json={
                "content": "可以用的旧记忆",
                "allow_writing": False,
            },
            headers=HEADERS,
        )
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.error == "context_changed" and task.adopted_at is None


@pytest.mark.asyncio
async def test_unchanged_memory_save_and_empty_cleanup_do_not_discard_writing(
    settings,
    engine,
    db,
    client,
    owner,
    project,
):
    item = remember(client, "已经保存的偏好", allow_writing=True)
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        response = client.put(
            f"/api/memories/{item['id']}",
            json={
                "content": "已经保存的偏好",
                "allow_writing": True,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["revision"] == item["revision"]
        response = client.post(
            "/api/chat-messages/clear",
            json={
                "start": "1900-01-01T00:00:00Z",
                "end": "1900-01-02T00:00:00Z",
            },
            headers=HEADERS,
        )
        assert response.json() == {"deleted_count": 0}
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_optional_memories_do_not_block_a_writing_request_that_fits(
    settings,
    engine,
    db,
    client,
    owner,
    project,
):
    remember(client, "可选的长记忆" * 100, allow_writing=True)
    task_id = pending_task(db, owner, project)
    task = db.get(Task, task_id)
    snapshot = _snapshot(db, task, db.get(Project, task.project_id), settings)
    request_settings = settings.model_copy(
        update={"model_max_output_tokens": settings.writing_output_tokens}
    )
    size = _request_size(
        _reply_request(
            request_settings,
            snapshot["instructions"],
            snapshot["messages"],
            WritingResult,
        )
    )
    snapshot["instructions"] += "x" * (MAX_INPUT_BYTES - size - 50)
    task.input_snapshot = snapshot
    db.commit()

    async def generate(*args):
        assert args[-2] == snapshot["instructions"]
        assert (
            _request_size(
                _reply_request(
                    request_settings,
                    args[-2],
                    args[-1],
                    WritingResult,
                )
            )
            <= MAX_INPUT_BYTES
        )
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_existing_work_chat_can_adopt_memory_and_reply(settings, engine, db, owner, project):
    task_id = message(db, 1, "回复时请不要用昵称")

    async def generate(*args):
        return Dialogue(
            reply="好的。",
            opinions=[],
            commission=None,
            memories=[
                MemoryCandidate(
                    content="用户交流时不喜欢昵称。",
                    kind="preference",
                    evidence="请不要用昵称",
                )
            ],
        )

    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.scalar(select(Memory)).content == "用户交流时不喜欢昵称。"
