"""Diary and chat share adopted life, while delivery and manuscript remain separate."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_author import pending_task
from test_works import HEADERS, document
from test_works import owner as owner

from lisa.author import _snapshot, execute_author
from lisa.chat import claim_outbound, execute_chat, finish_delivery, ingest_message
from lisa.dialogue import Conversation, Dialogue
from lisa.life import LifeCandidate, LifeMention
from lisa.models import Chapter, ChapterVersion, ChatMessage, Memory, OutboundMessage, Project, Task

EVENT = "下午在旧书店买了一本有批注的诗集。"
OCCURRED = datetime(2026, 1, 10, 7, 0, tzinfo=UTC)


@pytest.fixture
def diary(client, owner):
    response = client.post(
        "/api/projects", json={"title": "小莎的日常", "kind": "diary"}, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


def event(content=EVENT, **changes):
    return LifeCandidate(
        **{
            "content": content,
            "nature": "event",
            "occurred_at": OCCURRED,
            "evidence": content,
            **changes,
        }
    )


def result(content=EVENT, events=None):
    return {
        "title": "旧书店",
        "paragraphs": [content],
        "summary": "LISA在旧书店买到一本诗集。",
        "plot_state": "诗集已带回家。",
        "life_events": [
            item.model_dump() for item in (events if events is not None else [event(content)])
        ],
    }


def chat(db, number, content="今天怎么样？"):
    task_id = ingest_message(
        db,
        bot_id="diary-test",
        message_id=str(number),
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content=content,
    )
    db.commit()
    return task_id


@pytest.mark.asyncio
async def test_cancelled_tail_is_not_marked_as_shared_when_inflight_head_succeeds(
    settings, engine, db, client, owner,
):
    task_id = chat(db, 901)

    async def generate(*args):
        return Conversation(reply="今天去逛书店了。\n" + EVENT,
                            messages=["今天去逛书店了。", EVENT], life_events=[event()])

    await execute_chat(settings, engine, task_id, generate=generate)
    first = claim_outbound(db, "720000001")
    first_id = first.id
    db.commit()
    chat(db, 902, content="我想换个话题")
    finish_delivery(db, first_id, "sent", "synthetic-first-part")
    db.commit()
    db.expire_all()
    item = db.scalar(select(Memory).where(Memory.kind == "life"))
    assert item is not None and item.shared_at is None


@pytest.mark.asyncio
async def test_diary_adoption_is_global_and_later_chat_only_marks_shared_after_delivery(
    settings,
    engine,
    db,
    client,
    owner,
    diary,
):
    task_id = pending_task(db, owner, diary)

    async def write(*args):
        assert "LISA视角" in args[-2] and "合成作品测试" in args[-2]
        assert "不替用户写现实日记" in args[-2]
        return result()

    await execute_author(settings, engine, task_id, generate=write)
    await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    memories = db.scalars(select(Memory).where(Memory.kind == "life")).all()
    assert len(memories) == 1
    item = memories[0]
    assert item.project_id is None and item.shared_at is None
    chapter = db.get(Chapter, db.get(Task, task_id).chapter_id)
    assert item.source_chapter_id == chapter.id and item.source_version == chapter.version
    assert db.get(ChapterVersion, (chapter.id, chapter.version)).task_id == task_id
    assert db.scalar(select(OutboundMessage)) is None

    async def share(*args):
        assert EVENT in args[-2] and str(item.id) in args[-2]
        return Dialogue(
            reply=EVENT,
            opinions=[],
            commission=None,
            life_mentions=[
                LifeMention(memory_id=item.id, evidence=EVENT),
            ],
        )

    chat_id = chat(db, 1)
    await execute_chat(settings, engine, chat_id, generate=share)
    db.expire_all()
    assert db.get(Memory, item.id).shared_at is None
    outgoing = claim_outbound(db, "720000001")
    assert outgoing.task_id == chat_id
    finish_delivery(db, outgoing.id, "sent", "synthetic-gateway-id")
    db.commit()
    assert db.get(Memory, item.id).shared_at is not None


@pytest.mark.asyncio
async def test_chat_life_exists_before_delivery_and_becomes_diary_context(
    settings,
    engine,
    db,
    client,
    owner,
):
    chat_id = chat(db, 1)

    async def talk(*args):
        return Conversation(reply=EVENT, life_events=[event()])

    await execute_chat(settings, engine, chat_id, generate=talk)
    db.expire_all()
    item = db.scalar(select(Memory).where(Memory.kind == "life"))
    assert item is not None and item.shared_at is None
    assert item.source_message_id is None and item.source_task_id == chat_id
    assert db.scalar(select(ChatMessage.id).where(ChatMessage.task_id == chat_id)) is None
    assert db.scalar(select(Project)) is None  # A life event never creates an extra diary project.
    response = client.post(
        "/api/projects",
        json={"title": "日常续记", "kind": "diary"},
        headers=HEADERS,
    )
    task_id = pending_task(db, owner, response.json())
    next_event = "回家发现书里夹着一张旧车票。"

    async def write(*args):
        assert EVENT in args[-2]
        assert '"shared_at": null' in args[-2]
        return result(next_event)

    await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    items = db.scalars(select(Memory).where(Memory.kind == "life")).all()
    assert {item.content for item in items} == {EVENT, next_event}
    assert all(item.shared_at is None for item in items)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["failed", "unknown"])
async def test_failed_or_unknown_chat_delivery_does_not_unmake_life_or_claim_shared(
    settings,
    engine,
    db,
    owner,
    delivery,
):
    chat_id = chat(db, 1)

    async def talk(*args):
        return Conversation(reply=EVENT, life_events=[event()])

    await execute_chat(settings, engine, chat_id, generate=talk)
    db.expire_all()
    outgoing = claim_outbound(db, "720000001")
    finish_delivery(db, outgoing.id, delivery)
    db.commit()
    assert db.scalar(select(Memory)).shared_at is None
    assert db.get(Task, chat_id).status == "completed"


@pytest.mark.asyncio
async def test_cancelled_diary_does_not_adopt_late_life(settings, engine, db, client, owner, diary):
    task_id = pending_task(db, owner, diary)

    async def write(*args):
        assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
        return result()

    await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.scalar(select(Chapter)) is None
    assert db.scalar(select(Memory)) is None


@pytest.mark.asyncio
async def test_diary_and_life_rollback_together(settings, engine, db, owner, diary, monkeypatch):
    from lisa import works

    task_id = pending_task(db, owner, diary)
    adopt = works.adopt_life_events

    def interrupted(*args, **kwargs):
        adopt(*args, **kwargs)
        raise RuntimeError("synthetic database transaction interruption")

    monkeypatch.setattr(works, "adopt_life_events", interrupted)

    async def write(*args):
        return result()

    with pytest.raises(RuntimeError, match="synthetic"):
        await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    assert db.scalar(select(Chapter)) is None
    assert db.scalar(select(ChapterVersion)) is None
    assert db.scalar(select(Memory)) is None
    assert db.get(Task, task_id).adopted_at is None


@pytest.mark.asyncio
async def test_novel_output_cannot_create_lisa_life(settings, engine, db, client, owner):
    response = client.post(
        "/api/projects",
        json={"title": "小说中的买书人", "kind": "novel"},
        headers=HEADERS,
    )
    task_id = pending_task(db, owner, response.json())

    async def write(*args):
        assert "life_events必须为空" in args[-2]
        return result()

    await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.scalar(select(Memory)) is None


@pytest.mark.asyncio
async def test_diary_rewrite_only_adopts_new_fragment_and_manual_save_does_not_derive_life(
    settings,
    engine,
    db,
    client,
    owner,
    diary,
):
    task_id = pending_task(db, owner, diary)

    async def write(*args):
        return result(EVENT + "旧的结尾。", events=[])

    await execute_author(settings, engine, task_id, generate=write)
    db.expire_all()
    chapter_id = db.get(Task, task_id).chapter_id
    rewrite_id = pending_task(db, owner, diary, kind="rewrite", chapter_id=chapter_id)
    new = "晚间把车票夹回诗集中。"

    async def rewrite(*args):
        return result(new, events=[event(EVENT), event(new)])

    await execute_author(settings, engine, rewrite_id, generate=rewrite)
    db.expire_all()
    assert [item.content for item in db.scalars(select(Memory))] == [new]
    entry = db.scalar(select(Memory))
    assert entry.source_version == 2
    response = client.put(
        f"/api/chapters/{chapter_id}",
        json={
            "title": "新的日记标题",
            "content": document("我想象自己搬到了海边。"),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.scalar(select(Memory)).content == new
    source = client.get(f"/api/memories/{entry.id}/source").json()
    assert source["status"] == "chapter" and source["chapter"]["version"] == 2
    assert new in source["chapter"]["content"]
    assert "海边" not in source["chapter"]["content"]


@pytest.mark.asyncio
async def test_life_mention_must_be_in_actual_context_and_reply(settings, engine, db, owner):
    item = Memory(
        user_id=owner,
        kind="life",
        content=EVENT,
        life_kind="event",
        occurred_at=OCCURRED,
        allow_chat=False,
        allow_writing=True,
    )
    db.add(item)
    db.commit()
    chat_id = chat(db, 1)

    async def talk(*args):
        assert EVENT not in args[-2]
        return Conversation(
            reply=EVENT, life_mentions=[LifeMention(memory_id=item.id, evidence=EVENT)]
        )

    await execute_chat(settings, engine, chat_id, generate=talk)
    db.expire_all()
    outgoing = claim_outbound(db, "720000001")
    finish_delivery(db, outgoing.id, "sent", "synthetic-id")
    db.commit()
    assert db.get(Memory, item.id).shared_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scene", "rewrite"])
async def test_diary_retry_filters_excluded_source_but_keeps_explicit_rewrite_target(
    settings,
    engine,
    db,
    client,
    owner,
    diary,
    kind,
):
    first = pending_task(db, owner, diary)

    async def initial(*args):
        return result(EVENT + "旧的结尾。", events=[event()])

    await execute_author(settings, engine, first, generate=initial)
    db.expire_all()
    entry = db.scalar(select(Memory))
    chapter_id = db.get(Task, first).chapter_id
    next_id = pending_task(
        db,
        owner,
        diary,
        kind=kind,
        chapter_id=chapter_id if kind == "rewrite" else None,
    )
    task = db.get(Task, next_id)
    task.input_snapshot = _snapshot(db, task, db.get(Project, task.project_id), settings)
    db.commit()
    assert EVENT in json.loads(task.input_snapshot["messages"][0]["content"])["context"]
    assert client.delete(f"/api/memories/{entry.id}", headers=HEADERS).status_code == 204

    async def after_deletion(*args):
        payload = json.loads(args[-1][0]["content"])
        assert payload["context"] == ""
        assert payload["target_text"] == ("旧的结尾。" if kind == "rewrite" else None)
        return result("新的结尾。", events=[])

    await execute_author(settings, engine, next_id, generate=after_deletion)
    db.expire_all()
    assert db.get(Task, next_id).status == "completed"
    assert not db.get(Memory, entry.id).active
