from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from lisa.life import LifeCandidate, adopt_life_events, mark_life_shared, queue_life_shares
from lisa.memory import memory_context
from lisa.models import (
    Account,
    Chapter,
    ChapterVersion,
    ChatMessage,
    LifeShare,
    Memory,
    Project,
    Task,
)

HEADERS = {"X-LISA-Request": "1"}
EVENT_TIME = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
TEXT = "下午在旧书店买了一本有批注的诗集。晚上做梦飞过海面。"


def document(text):
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
        ],
    }


def event(**changes):
    return LifeCandidate.model_validate(
        {
            "content": "在旧书店买了一本有批注的诗集。",
            "nature": "event",
            "occurred_at": EVENT_TIME,
            "evidence": "下午在旧书店买了一本有批注的诗集。",
            **changes,
        }
    )


@pytest.fixture
def owner(client, invites):
    response = client.post(
        "/api/auth/register",
        headers=HEADERS,
        json={
            "username": "life_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


@pytest.fixture
def chat_source(db, owner):
    task = Task(user_id=owner, kind="chat", status="completed")
    db.add(task)
    db.flush()
    message = ChatMessage(
        user_id=owner, task_id=task.id, role="assistant", content=TEXT, sent_at=EVENT_TIME
    )
    db.add(message)
    db.commit()
    return message


@pytest.fixture
def diary_source(db, owner):
    project = Project(user_id=owner, title="小莎的日记", kind="diary")
    db.add(project)
    db.flush()
    chapter = Chapter(
        project_id=project.id, title="旧书店", position=1, content=document(TEXT), version=1
    )
    db.add(chapter)
    db.flush()
    version = ChapterVersion(
        chapter_id=chapter.id,
        version=1,
        title=chapter.title,
        content=chapter.content,
        source="generated",
    )
    db.add(version)
    db.commit()
    return version


@pytest.mark.parametrize("nature", ["plan", "imagination", "character", "background_change"])
def test_non_events_never_enter_life(db, owner, chat_source, nature):
    assert (
        adopt_life_events(db, owner, [event(nature=nature)], TEXT, source_message_id=chat_source.id)
        == []
    )
    assert list(db.scalars(select(Memory))) == []


def test_event_and_dream_are_grounded_and_deduplicated(db, owner, chat_source):
    dream = event(content="飞过海面。", nature="dream", evidence="晚上做梦飞过海面。")
    ids = adopt_life_events(
        db, owner, [event(), dream, event()], TEXT, source_message_id=chat_source.id
    )
    db.commit()
    assert len(ids) == 2
    assert (
        adopt_life_events(db, owner, [event(), dream], TEXT, source_message_id=chat_source.id)
        == ids
    )
    items = list(db.scalars(select(Memory).order_by(Memory.content)))
    assert {item.life_kind for item in items} == {"event", "dream"}
    assert next(item for item in items if item.life_kind == "dream").content.startswith(
        "做过一个梦："
    )
    assert all(
        item.project_id is None and item.allow_writing and item.shared_at is None for item in items
    )
    context = memory_context(db, owner, purpose="writing")
    assert '"shared_at": null' in context and str(ids[0]) in context and "2026-01-02" in context


def test_missing_evidence_future_event_and_wrong_source_rejected(db, owner, chat_source):
    invalid = [event(evidence="原文没有"), event(occurred_at=datetime.now(UTC) + timedelta(days=1))]
    assert adopt_life_events(db, owner, invalid, TEXT, source_message_id=chat_source.id) == []
    assert (
        adopt_life_events(db, owner, [event()], TEXT + "伪造", source_message_id=chat_source.id)
        == []
    )
    other = Account(username="life_other", password_hash="unused")
    db.add(other)
    db.flush()
    assert adopt_life_events(db, other.id, [event()], TEXT, source_message_id=chat_source.id) == []
    chat_source.role = "user"
    db.flush()
    assert adopt_life_events(db, owner, [event()], TEXT, source_message_id=chat_source.id) == []


def test_life_adoption_rolls_back_with_source(db, owner, chat_source):
    ids = adopt_life_events(db, owner, [event()], TEXT, source_message_id=chat_source.id)
    assert ids
    db.rollback()
    assert list(db.scalars(select(Memory))) == []


def test_diary_source_version_is_immutable_and_edit_does_not_rewrite(
    client, db, owner, diary_source
):
    ids = adopt_life_events(db, owner, [event()], TEXT, chapter=diary_source)
    db.commit()
    chapter = db.get(Chapter, diary_source.chapter_id)
    chapter.content, chapter.title, chapter.version = document("后来改写的日记。"), "新标题", 2
    db.add(
        ChapterVersion(
            chapter_id=chapter.id,
            version=2,
            title=chapter.title,
            content=chapter.content,
            source="web",
        )
    )
    db.commit()
    source = client.get(f"/api/memories/{ids[0]}/source").json()
    assert source == {
        "status": "chapter",
        "chapter": {
            "id": str(chapter.id),
            "title": "旧书店",
            "version": 1,
            "content": TEXT,
        },
    }
    response = client.put(
        f"/api/memories/{ids[0]}",
        headers=HEADERS,
        json={
            "kind": "life",
            "content": "买的是散文集。",
            "life_kind": "event",
            "occurred_at": EVENT_TIME.isoformat(),
            "allow_writing": True,
        },
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Chapter, chapter.id).content == document("后来改写的日记。")
    assert adopt_life_events(db, owner, [event()], TEXT, chapter=diary_source) == []
    assert "买的是散文集" in memory_context(db, owner)
    assert "有批注的诗集" not in memory_context(db, owner)


def test_diary_source_requires_own_saved_diary_version(db, owner, diary_source):
    other = Account(username="diary_other", password_hash="unused")
    db.add(other)
    db.flush()
    assert adopt_life_events(db, other.id, [event()], TEXT, chapter=diary_source) == []
    assert adopt_life_events(db, owner, [event()], TEXT + "伪造", chapter=diary_source) == []
    chapter = db.get(Chapter, diary_source.chapter_id)
    project = db.get(Project, chapter.project_id)
    project.kind = "novel"
    db.flush()
    assert adopt_life_events(db, owner, [event()], TEXT, chapter=diary_source) == []


@pytest.mark.parametrize("source_kind", ["chat", "diary"])
def test_deleted_life_does_not_revive_from_original_source(
    client, db, owner, chat_source, source_kind
):
    if source_kind == "diary":
        project = Project(user_id=owner, title="日记", kind="diary")
        db.add(project)
        db.flush()
        chapter = Chapter(
            project_id=project.id, title="旧书店", position=1, content=document(TEXT), version=1
        )
        db.add(chapter)
        db.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version=1,
            title=chapter.title,
            content=chapter.content,
            source="generated",
        )
        db.add(version)
        db.flush()
        args = {"chapter": version}
    else:
        args = {"source_message_id": chat_source.id}
    ids = adopt_life_events(db, owner, [event()], TEXT, **args)
    db.commit()
    assert client.delete(f"/api/memories/{ids[0]}", headers=HEADERS).status_code == 204
    db.expire_all()
    assert adopt_life_events(db, owner, [event()], TEXT, **args) == []
    assert memory_context(db, owner) == ""


def test_share_only_on_confirmation_and_correct_revision(client, db, owner, chat_source):
    ids = adopt_life_events(db, owner, [event()], TEXT, source_message_id=chat_source.id)
    queue_life_shares(db, owner, chat_source.task_id, ids + ids)
    db.commit()
    item = db.get(Memory, ids[0])
    assert item.shared_at is None and len(list(db.scalars(select(LifeShare)))) == 1
    mark_life_shared(db, chat_source.task_id, EVENT_TIME)
    db.commit()
    assert db.get(Memory, ids[0]).shared_at == EVENT_TIME
    body = {
        "kind": "life",
        "content": item.content,
        "life_kind": "event",
        "occurred_at": EVENT_TIME.isoformat(),
        "allow_writing": False,
    }
    assert (
        client.put(f"/api/memories/{ids[0]}", headers=HEADERS, json=body).json()["shared_at"]
        is not None
    )
    body["content"] = "更正为买散文集。"
    assert (
        client.put(f"/api/memories/{ids[0]}", headers=HEADERS, json=body).json()["shared_at"]
        is None
    )
    mark_life_shared(db, chat_source.task_id, EVENT_TIME + timedelta(minutes=1))
    db.commit()
    assert db.get(Memory, ids[0]).shared_at is None


def test_queue_rejects_foreign_inactive_and_disallowed_life(db, owner, chat_source):
    other = Account(username="sharing_other", password_hash="unused")
    db.add(other)
    db.flush()
    items = [
        Memory(
            user_id=other.id,
            kind="life",
            content="其他账号",
            life_kind="event",
            occurred_at=EVENT_TIME,
        ),
        Memory(
            user_id=owner,
            kind="life",
            content="已删除",
            life_kind="event",
            occurred_at=EVENT_TIME,
            active=False,
        ),
        Memory(
            user_id=owner,
            kind="life",
            content="不用于聊天",
            life_kind="event",
            occurred_at=EVENT_TIME,
            allow_chat=False,
        ),
        Memory(user_id=owner, kind="fact", content="普通事实"),
    ]
    db.add_all(items)
    db.flush()
    queue_life_shares(db, owner, chat_source.task_id, [item.id for item in items])
    queue_life_shares(db, other.id, chat_source.task_id, [items[0].id])
    assert list(db.scalars(select(LifeShare))) == []


def test_manual_life_validation_and_timeline_sort(client, owner):
    payload = {
        "content": "去旧书店。",
        "kind": "life",
        "life_kind": "event",
        "occurred_at": EVENT_TIME.isoformat(),
    }
    assert (
        client.post(
            "/api/memories", headers=HEADERS, json={"content": "无时间", "kind": "life"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/memories", headers=HEADERS, json={**payload, "occurred_at": "2026-01-02T15:00:00"}
        ).status_code
        == 422
    )
    first = client.post(
        "/api/memories", headers=HEADERS, json={**payload, "shared_at": EVENT_TIME.isoformat()}
    )
    assert first.status_code == 201 and first.json()["shared_at"] is None
    second = client.post(
        "/api/memories",
        headers=HEADERS,
        json={
            **payload,
            "content": "去公园。",
            "occurred_at": (EVENT_TIME + timedelta(hours=1)).isoformat(),
        },
    )
    assert second.status_code == 201
    assert [m["content"] for m in client.get("/api/memories?kind=life").json()] == [
        "去公园。",
        "去旧书店。",
    ]
