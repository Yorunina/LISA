import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_auth import HEADERS, PASSWORD, register
from test_author import pending_task
from test_chat import ingest
from test_works import RESULT
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.app import create_app
from lisa.auth import issue_invites
from lisa.author import execute_author
from lisa.chat import claim_outbound, execute_chat, finish_delivery
from lisa.model_calls import _finish
from lisa.models import (
    Account,
    ActivityPlan,
    BudgetDay,
    Chapter,
    ChapterVersion,
    Character,
    CharacterParticipation,
    ChatMessage,
    ContactPreference,
    GuideNotice,
    IndexChunk,
    IndexState,
    Invite,
    LifeShare,
    LoginSession,
    Memory,
    MemoryUse,
    ModelCall,
    OutboundMessage,
    Persona,
    PersonaVersion,
    Project,
    QQBinding,
    RetrievalReference,
    ServiceCode,
    ServiceEvent,
    Task,
)
from lisa.security import hash_token

QQ = "720000001"


def delete_account(client, *, password=PASSWORD, headers=HEADERS):
    return client.request(
        "DELETE", "/api/auth/account", json={"password": password}, headers=headers
    )


def test_delete_cleans_owned_data_and_preserves_other_users_and_budget(
    settings, engine, client, db, owner, project, chapter, invites
):
    now = datetime.now(UTC)
    task_id = pending_task(db, owner, project)
    chat_id = ingest(db, "delete-account", qq_id=QQ)
    task = db.get(Task, task_id)
    doing_job = task.queue_job_id
    queued_job = db.get(Task, chat_id).queue_job_id
    persona = db.scalar(select(Persona).where(Persona.user_id == owner))
    memory = Memory(user_id=owner, kind="life", content="合成生活记忆", source_chapter_id=chapter)
    character = Character(project_id=UUID(project["id"]), name="角色", prompt="合成角色")
    state = IndexState(
        user_id=owner, source_kind="chapter", source_id=chapter, revision=1, space="synthetic"
    )
    db.add_all(
        [
            memory,
            character,
            state,
            BudgetDay(day=now.date(), spent_usd=Decimal("0.1"), reserved_usd=Decimal("0.03")),
        ]
    )
    db.flush()
    call = ModelCall(
        task_id=task_id,
        user_id=owner,
        budget_day=now.date(),
        state="reserved",
        reserved_usd=Decimal("0.03"),
        configuration={"model": "synthetic"},
    )
    db.add_all(
        [
            call,
            ContactPreference(user_id=owner),
            PersonaVersion(
                persona_id=persona.id, version=1, name=persona.name, prompt=persona.prompt
            ),
            ActivityPlan(
                user_id=owner,
                project_id=UUID(project["id"]),
                goal="合成计划",
                due_at=now + timedelta(hours=1),
                activity_at=now,
            ),
            CharacterParticipation(
                task_id=task_id,
                position=1,
                character_id=character.id,
                revision=1,
                state="completed",
            ),
            MemoryUse(memory_id=memory.id, task_id=task_id),
            LifeShare(memory_id=memory.id, task_id=task_id, memory_revision=1),
            IndexChunk(
                state_id=state.id,
                chunk_index=0,
                revision=1,
                space="synthetic",
                start=0,
                end=2,
                embedding=[1.0, 0.0],
            ),
            RetrievalReference(
                task_id=task_id,
                position=1,
                source_kind="chapter",
                source_id=chapter,
                revision=1,
                space="synthetic",
                chunk_index=0,
                start=0,
                end=2,
            ),
            OutboundMessage(user_id=owner, task_id=task_id, qq_id=QQ, content="合成待发送结果"),
            ServiceCode(
                code_hash=hash_token("unbound-code"),
                purpose="bind",
                qq_id=QQ,
                expires_at=now + timedelta(minutes=5),
            ),
            ServiceEvent(
                id="delete-account-event",
                bot_id="910000001",
                message_id="delete-account",
                qq_id=QQ,
                sent_at=now,
            ),
            GuideNotice(qq_id=QQ, stage="bind", sent_at=now),
            GuideNotice(qq_id="720000002", stage="bind", sent_at=now),
        ]
    )
    db.execute(text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": doing_job})
    db.flush()
    call_id = call.id
    # Consuming all seats makes the no-refund policy observable after deletion.
    issue_invites(db, 17)
    db.commit()
    old_cookies = dict(client.cookies)

    with TestClient(create_app(settings)) as other:
        other_id = UUID(register(other, invites[1], "other_reader").json()["id"])
        response = delete_account(client)
        assert response.status_code == 204, response.text
        assert "max-age=0" in response.headers["set-cookie"].lower()
        assert other.get("/api/auth/me").json()["id"] == str(other_id)
        assert register(other, invites[0], "replacement_reader").status_code == 400

    client.cookies.update(old_cookies)
    assert client.get("/api/auth/me").status_code == 401
    db.expire_all()
    assert db.scalars(select(Account.id)).all() == [other_id]
    assert set(db.scalars(select(LoginSession.user_id))) == {other_id}
    for model in (
        QQBinding,
        ContactPreference,
        Persona,
        PersonaVersion,
        Project,
        Character,
        CharacterParticipation,
        ActivityPlan,
        Chapter,
        ChapterVersion,
        Task,
        ChatMessage,
        Memory,
        MemoryUse,
        LifeShare,
        IndexState,
        IndexChunk,
        RetrievalReference,
        ModelCall,
        OutboundMessage,
        ServiceCode,
        ServiceEvent,
    ):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__
    assert db.scalars(select(GuideNotice.qq_id)).all() == ["720000002"]
    invite = db.get(Invite, hash_token(invites[0]))
    assert invite.used_by is None and invite.consumed
    with pytest.raises(ValueError):
        issue_invites(db, 1)
    db.rollback()

    shell = db.execute(
        text(
            "SELECT args, lock, queueing_lock, abort_requested FROM procrastinate_jobs WHERE id=:id"
        ),
        {"id": doing_job},
    ).one()
    assert tuple(shell) == ({}, None, None, True)
    assert (
        db.scalar(text("SELECT count(*) FROM procrastinate_jobs WHERE id=:id"), {"id": queued_job})
        == 0
    )
    assert (
        db.scalar(
            text("SELECT count(*) FROM procrastinate_events WHERE job_id=:id"), {"id": doing_job}
        )
        == 0
    )
    db.rollback()
    # A remote response can arrive after its identifiable ledger row has been erased.
    _finish(engine, call_id, state="succeeded", charged=Decimal("0.01"))
    day = db.get(BudgetDay, now.date())
    assert day.reserved_usd == 0 and day.spent_usd == Decimal("0.13")


def test_delete_requires_session_same_origin_header_and_correct_password(client, db, owner):
    assert delete_account(client, headers={}).status_code == 403
    assert delete_account(client, password="wrong-password").status_code == 403
    assert client.get("/api/auth/me").status_code == 200
    assert db.get(Account, owner) is not None and db.get(QQBinding, owner) is not None
    client.cookies.clear()
    assert delete_account(client).status_code == 401


@pytest.mark.asyncio
async def test_deletion_discards_inflight_writing_chat_and_delivery(
    settings, engine, client, db, owner, project, chapter
):
    writing_id = pending_task(db, owner, project)
    chat_id = ingest(db, "inflight-delete", qq_id=QQ)
    delivery_task = Task(user_id=owner, kind="chat", status="completed")
    db.add(delivery_task)
    db.flush()
    db.add(
        OutboundMessage(user_id=owner, task_id=delivery_task.id, qq_id=QQ, content="合成在途消息")
    )
    db.commit()
    delivery = claim_outbound(db, QQ)
    message_id = delivery.id
    db.commit()
    writing_started, chat_started, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def writing(*args):
        writing_started.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            pass  # Simulate a provider that still returns a late result.
        return RESULT

    async def chatting(*args):
        chat_started.set()
        await released.wait()
        return "合成迟到回复"

    running = asyncio.gather(
        execute_author(settings, engine, writing_id, generate=writing),
        execute_chat(settings, engine, str(chat_id), generate=chatting),
    )
    try:
        await asyncio.wait_for(asyncio.gather(writing_started.wait(), chat_started.wait()), 3)
        assert delete_account(client).status_code == 204
    finally:
        released.set()
    await asyncio.wait_for(running, 3)
    db.expire_all()
    finish_delivery(db, message_id, "sent", "synthetic-gateway-id")
    db.commit()
    for model in (Account, Task, Chapter, ChapterVersion, ChatMessage, Memory, OutboundMessage):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_failed_delete_rolls_back_account_and_related_cleanup(
    client, db, owner, project, monkeypatch
):
    task_id = pending_task(db, owner, project)
    job_id = db.get(Task, task_id).queue_job_id
    db.commit()

    def fail_commit(_session):
        raise RuntimeError("synthetic commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            delete_account(client)
    db.expire_all()
    assert client.get("/api/auth/me").status_code == 200
    assert db.get(Account, owner) is not None
    assert db.get(Project, UUID(project["id"])) is not None
    assert db.get(Task, task_id) is not None
    assert (
        db.scalar(text("SELECT count(*) FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}) == 1
    )
