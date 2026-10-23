from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from lisa.memory import (
    MemoryCandidate,
    adopt_candidates,
    memory_context,
    record_chat_references,
    usable_chat_clause,
)
from lisa.models import Account, ChatMessage, Memory, OutboundMessage, Project, Task

HEADERS = {"X-LISA-Request": "1"}


@pytest.fixture
def owner(client, invites):
    response = client.post(
        "/api/auth/register",
        headers=HEADERS,
        json={
            "username": "memory_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


@pytest.fixture
def conversation(db, owner):
    task = Task(user_id=owner, kind="chat", status="completed")
    db.add(task)
    db.flush()
    now = datetime.now(UTC)
    messages = [
        ChatMessage(
            user_id=owner, task_id=task.id, role="user", content="我喜欢雨天散步。", sent_at=now
        ),
        ChatMessage(
            user_id=owner,
            task_id=task.id,
            role="assistant",
            content="我记住你喜欢雨天散步了。",
            sent_at=now,
        ),
    ]
    db.add_all(messages)
    db.add(
        OutboundMessage(
            user_id=owner, task_id=task.id, qq_id="710000001", content=messages[1].content
        )
    )
    db.commit()
    return messages


def candidate(**changes):
    return MemoryCandidate.model_validate(
        {
            "content": "用户喜欢雨天散步。",
            "kind": "preference",
            "evidence": "我喜欢雨天散步。",
            **changes,
        }
    )


def create_memory(client, **changes):
    response = client.post(
        "/api/memories",
        headers=HEADERS,
        json={
            "content": "用户喜欢雨天散步。",
            "kind": "preference",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_manual_memory_validation_filters_and_epoch(client, db, owner):
    assert (
        client.post(
            "/api/memories",
            headers=HEADERS,
            json={
                "content": "喜欢安静",
                "kind": "inference",
            },
        ).status_code
        == 422
    )
    item = create_memory(client)
    assert item["revision"] == 1 and not item["allow_writing"]
    assert client.get("/api/memories", params={"kind": "fact"}).json() == []
    assert len(client.get("/api/memories", params={"query": "雨天"}).json()) == 1
    assert client.get(f"/api/memories/{item['id']}/source").json() == {
        "status": "manual",
        "message": None,
    }
    db.expire_all()
    assert db.get(Account, owner).memory_revision == 2


def test_memories_and_sources_are_account_isolated(client, db, owner, conversation):
    other = Account(username="other_reader", password_hash="unused")
    db.add(other)
    db.flush()
    project = Project(user_id=other.id, title="其他人的作品")
    source = ChatMessage(
        user_id=other.id, role="user", content="私密原话", sent_at=datetime.now(UTC)
    )
    db.add_all([project, source])
    db.flush()
    memory = Memory(user_id=other.id, source_message_id=source.id, kind="fact", content="私密记忆")
    db.add(memory)
    db.commit()
    assert client.get("/api/memories").json() == []
    assert client.get(f"/api/memories/{memory.id}/source").status_code == 404
    assert client.delete(f"/api/memories/{memory.id}", headers=HEADERS).status_code == 404
    assert client.delete(f"/api/chat-messages/{source.id}", headers=HEADERS).status_code == 404
    for payload in ({"source_message_id": str(source.id)}, {"project_id": str(project.id)}):
        assert (
            client.post(
                "/api/memories", headers=HEADERS, json={"content": "跨用户引用", **payload}
            ).status_code
            == 404
        )
    assert [item["content"] for item in client.get("/api/chat-messages").json()] != ["私密原话"]
    assert memory_context(db, owner) == ""


@pytest.mark.parametrize("operation", ["correction", "deletion", "restriction"])
def test_source_task_excluded_and_never_reextracted(client, db, owner, conversation, operation):
    source, answer = conversation
    adopt_candidates(db, owner, None, [candidate(allow_writing=True)], [source.id])
    db.commit()
    item = client.get("/api/memories").json()[0]
    if operation == "deletion":
        response = client.delete(f"/api/memories/{item['id']}", headers=HEADERS)
    else:
        response = client.put(
            f"/api/memories/{item['id']}",
            headers=HEADERS,
            json={
                "content": "用户喜欢晴天散步。" if operation == "correction" else item["content"],
                "kind": "preference",
                "allow_writing": operation != "restriction",
            },
        )
    assert response.status_code in (200, 204)
    db.expire_all()
    assert list(db.scalars(select(ChatMessage).where(usable_chat_clause()))) == []
    assert db.get(ChatMessage, answer.id).content
    adopt_candidates(db, owner, None, [candidate()], [source.id])
    db.commit()
    entries = client.get("/api/memories").json()
    if operation == "deletion":
        assert entries == [] and memory_context(db, owner) == ""
    elif operation == "correction":
        assert len(entries) == 1 and "晴天" in memory_context(db, owner)
        assert "雨天" not in memory_context(db, owner)
    else:
        assert len(entries) == 1 and memory_context(db, owner, purpose="writing") == ""
    assert len(client.get("/api/chat-messages").json()) == 2


def test_deleted_chat_source_keeps_memory_and_cannot_be_adopted(client, db, owner, conversation):
    source = conversation[0]
    adopt_candidates(db, owner, None, [candidate()], [source.id])
    db.commit()
    item = client.get("/api/memories").json()[0]
    assert client.delete(f"/api/chat-messages/{source.id}", headers=HEADERS).status_code == 204
    db.expire_all()
    assert db.get(ChatMessage, source.id).content == ""
    assert client.get(f"/api/memories/{item['id']}/source").json() == {
        "status": "deleted",
        "message": None,
    }
    assert client.get("/api/memories").json()[0]["source_deleted"]
    assert "雨天" in memory_context(db, owner)
    adopt_candidates(db, owner, None, [candidate(content="迟到的新记忆")], [source.id])
    db.commit()
    assert len(client.get("/api/memories").json()) == 1


def test_time_cleanup_is_bounded_and_clears_outbound_copy(client, db, owner, conversation):
    source, answer = conversation
    adopt_candidates(db, owner, None, [candidate()], [source.id])
    db.add(
        ChatMessage(
            user_id=owner,
            role="user",
            content="保留旧消息",
            sent_at=source.sent_at - timedelta(days=2),
        )
    )
    db.commit()
    response = client.post(
        "/api/chat-messages/clear",
        headers=HEADERS,
        json={
            "start": (source.sent_at - timedelta(minutes=1)).isoformat(),
            "end": (source.sent_at + timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.json() == {"deleted_count": 2}
    db.expire_all()
    outbound = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == answer.task_id))
    assert outbound.content == "" and outbound.status == "deleted"
    assert [m["content"] for m in client.get("/api/chat-messages").json()] == ["保留旧消息"]
    assert len(client.get("/api/memories").json()) == 1


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-01T00:00:00", "2026-10-02T00:00:00"),
        ("2026-10-02T00:00:00Z", "2026-10-01T00:00:00Z"),
    ],
)
def test_clear_rejects_naive_or_reversed_time(client, owner, start, end):
    assert (
        client.post(
            "/api/chat-messages/clear", headers=HEADERS, json={"start": start, "end": end}
        ).status_code
        == 422
    )


def test_context_scope_purpose_and_serialized_budget(db, owner):
    first, second = (
        Project(user_id=owner, title="作品一"),
        Project(user_id=owner, title="作品二", is_current=False),
    )
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            Memory(user_id=owner, kind="fact", content="关系可用", allow_writing=True),
            Memory(
                user_id=owner,
                project_id=first.id,
                kind="fiction",
                content="本作品虚构",
                allow_writing=True,
            ),
            Memory(
                user_id=owner,
                project_id=second.id,
                kind="fiction",
                content="其他作品",
                allow_writing=True,
            ),
            Memory(user_id=owner, kind="fact", content="只用于聊天", allow_writing=False),
            Memory(user_id=owner, kind="fact", content="已删除", active=False, allow_writing=True),
            Memory(user_id=owner, kind="inference", content="较长的推测", basis="依据" * 500),
        ]
    )
    db.commit()
    context = memory_context(db, owner, first.id, "writing")
    assert "关系可用" in context and "本作品虚构" in context
    assert "其他作品" not in context and "只用于聊天" not in context and "已删除" not in context
    assert len(memory_context(db, owner, max_chars=100)) <= 100
    assert memory_context(db, owner, max_chars=1) == ""
    assert "本作品虚构" not in memory_context(db, owner)


def test_candidates_require_user_evidence_and_deduplicate(db, owner, conversation):
    source, answer = conversation
    before_epoch = db.get(Account, owner).memory_revision
    adopt_candidates(
        db,
        owner,
        None,
        [
            candidate(evidence="不存在的原话"),
            candidate(scope="project"),
            candidate(evidence=answer.content),
            candidate(),
            candidate(),
        ],
        [source.id, answer.id],
    )
    db.commit()
    items = list(db.scalars(select(Memory)))
    assert len(items) == 1 and items[0].source_message_id == source.id
    assert db.get(Account, owner).memory_revision == before_epoch


def test_duplicate_sources_and_later_echoes_are_excluded(client, db, owner, conversation):
    source = conversation[0]
    adopt_candidates(db, owner, None, [candidate()], [source.id])
    repeated_task, recall_task, echo_task, unrelated_task = [
        Task(user_id=owner, kind="chat", status="completed") for _ in range(4)
    ]
    db.add_all([repeated_task, recall_task, echo_task, unrelated_task])
    db.flush()
    repeated = ChatMessage(
        user_id=owner,
        task_id=repeated_task.id,
        role="user",
        content=source.content,
        sent_at=datetime.now(UTC),
    )
    recalled = ChatMessage(
        user_id=owner,
        task_id=recall_task.id,
        role="assistant",
        content="你喜欢雨天散步。",
        sent_at=datetime.now(UTC),
    )
    echoed = ChatMessage(
        user_id=owner,
        task_id=echo_task.id,
        role="assistant",
        content="继续聊聊你喜欢的雨天散步。",
        sent_at=datetime.now(UTC),
    )
    unrelated = ChatMessage(
        user_id=owner,
        task_id=unrelated_task.id,
        role="user",
        content="这是一条无关消息。",
        sent_at=datetime.now(UTC),
    )
    db.add_all([repeated, recalled, echoed, unrelated])
    db.flush()
    adopt_candidates(db, owner, None, [candidate()], [repeated.id])
    references = []
    memory_context(db, owner, references=references)
    record_chat_references(db, recall_task.id, references, [])
    record_chat_references(db, echo_task.id, [], [recalled.id])
    db.commit()
    item = client.get("/api/memories").json()[0]
    assert client.delete(f"/api/memories/{item['id']}", headers=HEADERS).status_code == 204
    db.expire_all()
    usable = list(db.scalars(select(ChatMessage.id).where(usable_chat_clause())))
    assert usable == [unrelated.id]
    adopt_candidates(db, owner, None, [candidate()], [source.id, repeated.id])
    db.commit()
    assert client.get("/api/memories").json() == []


def test_raw_source_history_usage_is_recorded(client, db, owner, conversation):
    source, original_answer = conversation
    adopt_candidates(db, owner, None, [candidate()], [source.id])
    later_task = Task(user_id=owner, kind="chat", status="completed")
    db.add(later_task)
    db.flush()
    answer = ChatMessage(
        user_id=owner,
        task_id=later_task.id,
        role="assistant",
        content="这段原始聊天提到雨天散步。",
        sent_at=datetime.now(UTC),
    )
    db.add(answer)
    record_chat_references(db, later_task.id, [], [original_answer.id])
    db.commit()
    item = client.get("/api/memories").json()[0]
    assert client.delete(f"/api/memories/{item['id']}", headers=HEADERS).status_code == 204
    db.expire_all()
    assert db.get(ChatMessage, answer.id).context_excluded
