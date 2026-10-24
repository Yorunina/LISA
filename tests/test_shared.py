import base64
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_chat import ingest
from test_retrieval import index_all
from test_retrieval import vector_settings as vector_settings
from test_works import HEADERS
from test_works import owner as owner

from lisa.chat import execute_chat
from lisa.dialogue import Conversation
from lisa.model_calls import BudgetExceeded, _finish, _reserve
from lisa.models import (
    Account,
    BudgetDay,
    IndexState,
    Memory,
    OutboundMessage,
    SharedAsset,
    SharedEntry,
    SharedPreference,
    SharedRevision,
    Task,
)
from lisa.recall import retrieve_context
from lisa.retrieval import _source
from lisa.shared import save_revision
from lisa.shared_jobs import (
    Analysis,
    enqueue_shared,
    execute_shared,
)
from lisa.shared_retrieval import outbound_segments, recall_shared, select_meme

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII="
)


def body(**changes):
    return {
        "kind": "meme",
        "title": "赛博晒太阳",
        "content": "自嘲地表达在网上放松。",
        "aliases": ["赛博日光浴"],
        "contribute": True,
        **changes,
    }


def create(client, **changes):
    response = client.post("/api/shared", json=body(**changes), headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def edit(client, item, **changes):
    data = {
        k: v
        for k, v in item.items()
        if k not in {"id", "muted", "editor_id", "updated_at", "human_edited"}
    }
    return client.put(
        f"/api/shared/{item['id']}", json={**data, "contribute": True, **changes}, headers=HEADERS
    )


def upload(client):
    response = client.post(
        "/api/shared/assets", content=PNG, headers={**HEADERS, "X-LISA-Contribute": "1"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def snapshot():
    return {
        "platform": "bilibili",
        "last_fetch": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "content_rank": [
            {
                "url": "https://www.bilibili.com/video/BV1234567890",
                "title": "合成热门视频",
                "desc": "只用于隔离测试的内容。",
                "topics": ["游戏"],
            }
        ],
        "danmaku_words": [{"word": "合成词", "count": 80}],
    }


def test_retired_terms_and_manual_radar_are_unavailable(client, db, owner):
    response = client.post("/api/shared", json=body(kind="term"), headers=HEADERS)
    assert response.status_code == 422
    response = client.post("/api/shared/radar/refresh", json={}, headers=HEADERS)
    assert response.status_code in {404, 405}
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_term_removal_migration_cleans_derived_data_only(client, db, owner):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from lisa.models import IndexChunk, RetrievalReference, SharedMute

    old = create(client, title="旧热词")
    keep = create(client, kind="meme", title="保留表情", asset_id=upload(client))
    old_id = UUID(old["id"])
    db.get(SharedEntry, old_id).kind = "term"
    state = IndexState(
        user_id=None, source_kind="shared", source_id=old_id, revision=1, space="test"
    )
    task = Task(
        user_id=None, kind="shared_analyze", request={"entry_id": str(old_id), "revision": 1}
    )
    db.add_all([state, task, SharedMute(user_id=owner, entry_id=old_id)])
    db.flush()
    state_id, task_id = state.id, task.id
    db.add_all(
        [
            IndexChunk(
                state_id=state_id,
                chunk_index=0,
                revision=1,
                space="test",
                start=0,
                end=5,
                embedding=[1.0, 0.0],
            ),
            RetrievalReference(
                task_id=task_id,
                position=0,
                source_kind="shared",
                source_id=old_id,
                revision=1,
                space="test",
                chunk_index=0,
                start=0,
                end=5,
            ),
        ]
    )
    db.flush()
    migration = import_module("migrations.versions.b741e065ab20_remove_shared_terms")
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.upgrade()
    db.expire_all()
    assert db.get(SharedEntry, old_id) is None
    assert db.get(SharedRevision, (old_id, 1)) is None
    assert db.get(SharedMute, (owner, old_id)) is None
    assert db.get(IndexState, state_id) is None
    assert db.get(IndexChunk, (state_id, 0)) is None
    assert db.get(RetrievalReference, (task_id, 0)) is None
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(SharedEntry, UUID(keep["id"])).asset_id
    assert db.get(Account, owner).active


def test_all_users_can_maintain_without_role_or_approval(client, db, owner, invites):
    item = create(client)
    assert not db.get(Account, owner).is_admin
    response = client.post(
        "/api/auth/register",
        headers=HEADERS,
        json={
            "username": "shared_friend",
            "password": "synthetic-password-2026",
            "invite_code": invites[1],
        },
    )
    assert response.status_code == 201
    friend = UUID(response.json()["id"])
    assert not db.get(Account, friend).is_admin
    assert client.get("/api/shared").json()[0]["content"] == item["content"]
    changed = edit(client, item, content="由另一名普通用户直接纠正。")
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert edit(client, item, content="过期页面不能覆盖").status_code == 409
    withdrawn = edit(client, changed.json(), status="withdrawn").json()
    assert client.get("/api/shared").json() == []
    assert len(client.get(f"/api/shared/{item['id']}/versions").json()) == 3
    restored = client.post(
        f"/api/shared/{item['id']}/restore",
        headers=HEADERS,
        json={
            "revision": withdrawn["revision"],
            "target_revision": 1,
        },
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 4
    client.post("/api/auth/logout", json={}, headers=HEADERS)
    assert client.get("/api/shared").status_code == 401
    assert client.post("/api/shared", json=body(), headers=HEADERS).status_code == 401


def test_consent_source_validation_and_image_dedup(client, db, owner):
    assert (
        client.post("/api/shared", json=body(contribute=False), headers=HEADERS).status_code == 422
    )
    assert (
        client.post("/api/shared", json=body(origin="public"), headers=HEADERS).status_code == 422
    )
    assert (
        client.post(
            "/api/shared", json=body(source_url="javascript:alert(1)"), headers=HEADERS
        ).status_code
        == 422
    )
    assert client.post("/api/shared/assets", content=PNG, headers=HEADERS).status_code == 422
    asset = upload(client)
    assert upload(client) == asset
    item = create(client, kind="meme", asset_id=asset)
    image = client.get(f"/api/shared/assets/{asset}")
    assert image.content == PNG and image.headers["content-type"] == "image/png"
    assert db.scalar(select(func.count()).select_from(SharedAsset)) == 1
    assert (
        client.post(
            "/api/shared/assets",
            content=b"<svg>unsafe</svg>",
            headers={**HEADERS, "X-LISA-Contribute": "1"},
        ).status_code
        == 422
    )
    assert item["status"] == "active"


def test_public_lookup_mutes_expiry_roles_and_private_isolation(client, db, owner, settings):
    item = create(client)
    other = Account(username="unrelated", password_hash="synthetic")
    db.add(other)
    db.flush()
    db.add(Memory(user_id=other.id, kind="fact", content="其他用户的私人秘密"))
    db.commit()
    context = recall_shared(db, settings, owner, "chat", "聊聊赛博日光浴")
    assert item["content"] in context.text
    assert "私人秘密" not in context.text
    assert not recall_shared(db, settings, owner, "role", item["title"]).text
    assert not recall_shared(db, settings, owner, "writing", item["title"]).text
    client.put(f"/api/shared/{item['id']}/mute", json={"muted": True}, headers=HEADERS)
    db.expire_all()
    assert not recall_shared(db, settings, owner, "chat", item["title"]).text
    assert recall_shared(db, settings, other.id, "chat", item["title"]).text
    client.put(f"/api/shared/{item['id']}/mute", json={"muted": False}, headers=HEADERS)
    changed = edit(client, item, expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert changed.status_code == 200
    db.expire_all()
    assert not recall_shared(db, settings, owner, "chat", item["title"]).text


@pytest.mark.asyncio
async def test_public_vectors_reuse_index_tables_without_private_scope(
    client, db, engine, owner, vector_settings
):
    item = create(client)
    assert await index_all(engine, vector_settings) == 1
    db.expire_all()
    state = db.scalar(select(IndexState).where(IndexState.source_kind == "shared"))
    state_id = state.id
    assert state.user_id is None and state.complete
    assert _source(db, "shared", UUID(item["id"]), owner) is None
    embed = AsyncMock(return_value=[[1.0, 0, 0]])
    result = await retrieve_context(
        vector_settings, engine, uuid4(), owner, None, "chat", "赛博日光浴", embed=embed
    )
    assert item["title"] in result.text
    embed.assert_awaited_once()
    changed = edit(client, item, content="新的公开解释")
    assert changed.status_code == 200
    db.expire_all()
    assert db.get(IndexState, state_id) is None
    assert "新的公开解释" in recall_shared(db, vector_settings, owner, "chat", item["title"]).text


def test_platform_ledger_caps_all_maintenance_and_global_budget(db, engine, owner, settings):
    common = Task(user_id=None, kind="shared_analyze", status="running")
    private = Task(user_id=owner, kind="chat", status="running")
    db.add_all([common, private])
    db.commit()
    config = settings.model_copy(update={"model_task_usd": Decimal(5), "model_concurrency": 8})
    call = _reserve(config, engine, common.id, None, Decimal("0.6"))
    _finish(engine, call, state="succeeded", charged=Decimal("0.6"))
    another = Task(user_id=None, kind="shared_analyze", status="running")
    db.add(another)
    db.commit()
    with pytest.raises(BudgetExceeded, match="shared maintenance"):
        _reserve(config, engine, another.id, None, Decimal("0.5"))
    private_call = _reserve(config, engine, private.id, owner, Decimal("0.6"))
    _finish(engine, private_call, state="succeeded", charged=Decimal("0.6"))
    with pytest.raises(BudgetExceeded, match="Daily model"):
        _reserve(
            config.model_copy(update={"model_daily_usd": Decimal("1.3")}),
            engine,
            another.id,
            None,
            Decimal("0.2"),
        )
    db.expire_all()
    assert db.get(BudgetDay, datetime.now(UTC).date()).spent_usd == Decimal("1.2")


@pytest.mark.asyncio
async def test_analysis_does_not_restore_withdrawn_or_changed_entry(
    client, db, engine, owner, settings
):
    item = create(client)
    task = Task(
        kind="shared_analyze",
        user_id=None,
        status="pending",
        request={"entry_id": item["id"], "revision": item["revision"]},
    )
    db.add(task)
    db.commit()

    async def generate(*args, **kwargs):
        with Session(engine) as session, session.begin():
            value = session.get(SharedEntry, UUID(item["id"]))
            value.status = "withdrawn"
            save_revision(session, value, human=True)
        return Analysis(content="迟到的模型结果", tags=[])

    await execute_shared(settings, engine, task.id, generate=generate)
    db.expire_all()
    assert db.get(Task, task.id).status == "failed"
    assert db.get(SharedEntry, UUID(item["id"])).content == item["content"]


@pytest.mark.asyncio
async def test_chat_selects_existing_meme_and_delivery_rechecks_withdrawal(
    client, db, engine, owner, settings
):
    asset = upload(client)
    item = create(
        client,
        kind="meme",
        title="合成开心表情",
        asset_id=asset,
        content="用于轻松开心的交流，不用于严肃倾诉。",
    )
    task_id = ingest(db, "shared-chat", qq_id="720000001", content="今天玩得很开心")

    async def generate(*args):
        assert "合成开心表情" in args[4]
        return Conversation(reply="听起来玩得不错。", meme_id=UUID(item["id"]))

    await execute_chat(settings, engine, str(task_id), generate=generate)
    db.expire_all()
    message = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert message.meme_id == UUID(item["id"])
    segments = outbound_segments(db, message)
    assert [s["type"] for s in segments] == ["text", "image"]
    assert segments[1]["data"]["file"].startswith("base64://")
    db.commit()
    changed = edit(client, item, status="withdrawn")
    assert changed.status_code == 200
    db.expire_all()
    assert len(outbound_segments(db, message)) == 1
    assert message.meme_id is None


def test_unknown_meme_or_personal_optout_cannot_be_sent(client, db, owner, settings):
    asset = upload(client)
    item = create(client, kind="meme", asset_id=asset)
    context = recall_shared(db, settings, owner, "chat", "开心")
    assert select_meme(db, owner, uuid4(), context) is None
    assert select_meme(db, owner, UUID(item["id"]), context)
    past = Task(user_id=owner, kind="chat", status="completed")
    db.add(past)
    db.flush()
    sent = OutboundMessage(
        user_id=owner,
        task_id=past.id,
        qq_id="720000001",
        content="合成",
        meme_id=UUID(item["id"]),
        meme_revision=item["revision"],
        status="sent",
        finished_at=datetime.now(UTC),
    )
    db.add(sent)
    db.commit()
    assert select_meme(db, owner, UUID(item["id"]), context) is None
    sent.finished_at = datetime.now(UTC) - timedelta(hours=3)
    db.commit()
    assert select_meme(db, owner, UUID(item["id"]), context) is not None
    client.put("/api/shared/preferences", json={"meme_mode": "off"}, headers=HEADERS)
    db.expire_all()
    assert select_meme(db, owner, UUID(item["id"]), context) is None
    assert db.get(SharedPreference, owner).meme_mode == "off"


def test_deleting_contributor_removes_private_versions_and_files_but_keeps_public(
    client, db, owner
):
    asset = upload(client)
    private = create(client, kind="meme", asset_id=asset)
    public = create(
        client, title="公开词语", origin="public", source_url="https://example.com/term"
    )
    response = client.request(
        "DELETE", "/api/auth/account", headers=HEADERS, json={"password": "synthetic-password-2026"}
    )
    assert response.status_code == 204, response.text
    db.expire_all()
    assert db.get(SharedEntry, UUID(private["id"])) is None
    assert db.get(SharedAsset, UUID(asset)) is None
    assert not list(
        db.scalars(select(SharedRevision).where(SharedRevision.entry_id == UUID(private["id"])))
    )
    assert db.get(SharedEntry, UUID(public["id"])) is not None


@pytest.mark.asyncio
async def test_explicit_chat_optout_is_applied_before_image_selection(
    client, db, engine, owner, settings
):
    item = create(client, kind="meme", asset_id=upload(client))
    task_id = ingest(db, "shared-optout", qq_id="720000001", content="以后不要发表情")

    async def generate(*args):
        return Conversation(
            reply="好，之后用文字交流。",
            meme_id=UUID(item["id"]),
            meme_preference="off",
            meme_preference_evidence="以后不要发表情",
        )

    await execute_chat(settings, engine, str(task_id), generate=generate)
    db.expire_all()
    assert db.get(SharedPreference, owner).meme_mode == "off"
    assert (
        db.scalar(select(OutboundMessage.meme_id).where(OutboundMessage.task_id == task_id)) is None
    )


@pytest.mark.asyncio
async def test_withdraw_during_chat_generation_rejects_old_public_answer(
    client, db, engine, owner, settings
):
    item = create(client)
    task_id = ingest(db, "shared-withdraw", qq_id="720000001", content="赛博日光浴是什么意思")

    async def generate(*args):
        with Session(engine) as session, session.begin():
            value = session.get(SharedEntry, UUID(item["id"]))
            value.status = "withdrawn"
            save_revision(session, value, human=True)
        return Conversation(reply="不应采用的过时解释")

    await execute_chat(settings, engine, str(task_id), generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).error == "context_changed"
    output = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert "不应采用" not in output.content


def test_private_history_is_erased_after_independent_public_replacement(client, db, owner):
    item = create(client, content="仅来自个人贡献的旧资料")
    response = edit(
        client,
        item,
        origin="public",
        content="已经有独立公开来源的新资料",
        source_url="https://example.com/independent",
    )
    assert response.status_code == 200
    deleted = client.request(
        "DELETE", "/api/auth/account", headers=HEADERS, json={"password": "synthetic-password-2026"}
    )
    assert deleted.status_code == 204
    db.expire_all()
    remaining = db.get(SharedEntry, UUID(item["id"]))
    assert remaining and remaining.origin == "public"
    versions = list(
        db.scalars(select(SharedRevision).where(SharedRevision.entry_id == remaining.id))
    )
    assert len(versions) == 1
    assert "个人贡献的旧资料" not in str(versions[0].data)


def test_platform_recovery_has_no_account_dependency_and_never_replays_unknown_calls(
    db, engine, settings
):
    from lisa.recovery import recover_tasks

    task = enqueue_shared(db, settings, "shared_analyze", {"entry_id": str(uuid4()), "revision": 1})
    task.status, task.started_at = "running", datetime.now(UTC)
    db.execute(
        text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": task.queue_job_id}
    )
    db.commit()
    assert recover_tasks(engine) == 1
    db.expire_all()
    assert db.get(Task, task.id).status == "pending"
    db.get(Task, task.id).status = "running"
    db.execute(
        text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": task.queue_job_id}
    )
    db.commit()
    _reserve(settings, engine, task.id, None, Decimal("0.01"))
    assert recover_tasks(engine) == 1
    db.expire_all()
    assert db.get(Task, task.id).status == "failed"
    assert db.get(Task, task.id).error == "model_unknown"
    assert recover_tasks(engine) == 0


def test_unchanged_save_normalizes_timezones_without_invalidating_or_freezing_source(
    client, db, owner
):
    row = SharedEntry(
        kind="meme",
        title="自动来源",
        content="公开简介",
        origin="public",
        source_url="https://example.com/video",
        source_time=datetime.now(UTC),
        status="active",
        revision=1,
    )
    db.add(row)
    save_revision(db, row, initial=True)
    state = IndexState(
        user_id=None, source_kind="shared", source_id=row.id, revision=1, space="synthetic"
    )
    db.add(state)
    db.commit()
    state_id = state.id
    item = client.get("/api/shared").json()[0]
    same_time = row.source_time.astimezone(timezone(timedelta(hours=8))).isoformat()
    response = edit(client, item, source_time=same_time)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert response.json()["human_edited"] is False
    db.expire_all()
    assert db.get(IndexState, state_id) is not None
