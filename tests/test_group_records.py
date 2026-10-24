"""Synthetic passive group records, explicit access and revocable source snapshots."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_onboarding import HEADERS, PASSWORD, signup

from lisa.__main__ import grant_admin
from lisa.group_records import ingest_group_message, purge_expired_messages, source_available
from lisa.models import (
    Account,
    ChatMessage,
    CollectedGroup,
    GroupGrant,
    GroupMessage,
    Memory,
    QQBinding,
    StyleCorpus,
    StyleProfile,
    Task,
)


@pytest.fixture
def group_admin(client, db, invites):
    account_id = UUID(signup(client, invites[0], "group_admin")["id"])
    user = db.get(Account, account_id)
    assert user.is_admin is False
    db.add(QQBinding(user_id=user.id, qq_id="700000001"))
    db.commit()
    grant_admin(db, "GROUP_ADMIN", "700000001")
    db.commit()
    return user


@pytest.fixture
def group_reader(client, invites):
    other = TestClient(client.app)
    data = signup(other, invites[1], "group_reader")
    yield other, UUID(data["id"])
    other.close()


def make_group(client, qq_id="800000001"):
    response = client.post(
        "/api/groups",
        json={"bot_id": "900000001", "qq_group_id": qq_id, "name": "合成测试群", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def allow(client, group_id, username="group_reader"):
    response = client.put(
        f"/api/groups/{group_id}/grants", json={"username": username}, headers=HEADERS
    )
    assert response.status_code == 200, response.text


def event(
    index,
    *,
    qq_id="710000001",
    content=None,
    name="合成人物",
    group_id="800000001",
    sent_at=None,
    kind="text",
):
    return SimpleNamespace(
        bot_id="900000001",
        group_id=group_id,
        message_id=str(index),
        qq_id=qq_id,
        name=name,
        text=content or f"第 {index} 条合成消息",
        sent_at=sent_at or datetime.now(UTC) - timedelta(minutes=2) + timedelta(seconds=index),
        kind=kind,
    )


def add_message(db, index, **kwargs):
    accepted = ingest_group_message(db, event(index, **kwargs))
    db.commit()
    return accepted


def snapshot(client, group_id, **kwargs):
    response = client.post(
        f"/api/groups/{group_id}/corpus", json={"qq_id": "710000001", **kwargs}, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_command_requires_existing_matching_binding(client, db, invites):
    account_id = UUID(signup(client, invites[0], "synthetic_owner")["id"])
    with pytest.raises(ValueError, match="绑定"):
        grant_admin(db, "synthetic_owner", "700000001")
    assert db.get(Account, account_id).is_admin is False
    db.add(QQBinding(user_id=account_id, qq_id="700000001"))
    db.commit()
    with pytest.raises(ValueError, match="不匹配"):
        grant_admin(db, "synthetic_owner", "700000002")
    assert db.get(QQBinding, account_id).qq_id == "700000001"
    with pytest.raises(ValueError, match="数字"):
        grant_admin(db, "synthetic_owner", "not-a-qq")
    grant_admin(db, "SYNTHETIC_OWNER", "700000001")
    db.commit()
    assert client.get("/api/auth/me").json()["is_admin"] is True


def test_only_admin_manages_groups_and_grants(client, db, group_admin, group_reader):
    reader, reader_id = group_reader
    group_id = make_group(client)
    assert reader.get("/api/groups").json() == {"is_admin": False, "groups": []}
    assert (
        reader.post(
            "/api/groups",
            json={"bot_id": "900000001", "qq_group_id": "800000002", "name": "无权创建"},
            headers=HEADERS,
        ).status_code
        == 403
    )
    assert reader.get(f"/api/groups/{group_id}/messages").status_code == 404
    assert (
        reader.put(
            f"/api/groups/{group_id}/grants", json={"username": "group_reader"}, headers=HEADERS
        ).status_code
        == 403
    )
    allow(client, group_id)
    assert reader.get("/api/groups").json()["groups"][0]["id"] == str(group_id)
    assert client.get(f"/api/groups/{group_id}/grants").json()["grants"] == [
        {"username": "group_reader", "user_id": str(reader_id)}
    ]
    assert reader.delete(f"/api/groups/{group_id}/messages", headers=HEADERS).status_code == 403
    assert (
        reader.patch(
            f"/api/groups/{group_id}", json={"enabled": False}, headers=HEADERS
        ).status_code
        == 403
    )


def test_group_ingest_is_passive_deduplicated_and_respects_pause(client, db, group_admin):
    assert add_message(db, 1) is False
    group_id = make_group(client)
    assert add_message(db, -1) is True  # OneBot message IDs may be signed int32.
    assert add_message(db, -1) is False
    assert add_message(db, 2, content="[图片]", kind="nontext") is True
    assert add_message(db, 3, group_id="800000002") is False
    assert add_message(db, 4, sent_at=datetime.now(UTC) - timedelta(days=31)) is False
    user_activity = db.get(Account, group_admin.id).last_active_at
    assert (
        client.patch(
            f"/api/groups/{group_id}", json={"enabled": False}, headers=HEADERS
        ).status_code
        == 200
    )
    assert add_message(db, 5) is False
    assert db.scalar(select(func.count()).select_from(GroupMessage)) == 2
    for model in (ChatMessage, Memory, Task):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    db.expire_all()
    assert db.get(Account, group_admin.id).last_active_at == user_activity
    assert client.get("/api/groups").json()["groups"][0]["message_count"] == 2


def test_granted_reader_can_search_other_people_and_literal_keywords(
    client, db, group_admin, group_reader
):
    reader, _ = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    for i in range(55):
        assert add_message(db, i, qq_id="720000002", name="新昵称" if i == 54 else "旧昵称")
    assert add_message(db, 56, content="包含 100%_test 的合成原文")
    found = reader.get(f"/api/groups/{group_id}/messages", params={"qq_id": "720000002"}).json()
    assert found["total"] == 55 and len(found["messages"]) == 50
    assert (
        len(
            reader.get(
                f"/api/groups/{group_id}/messages", params={"qq_id": "720000002", "offset": 50}
            ).json()["messages"]
        )
        == 5
    )
    percent = reader.get(f"/api/groups/{group_id}/messages", params={"keyword": "%_"}).json()
    assert percent["total"] == 1
    members = reader.get(f"/api/groups/{group_id}/members").json()
    assert members[0] == {"qq_id": "720000002", "name": "新昵称", "message_count": 55}
    assert (
        reader.get(
            f"/api/groups/{group_id}/messages", params={"start": "2026-10-24T10:00:00"}
        ).status_code
        == 422
    )


def test_group_snapshot_is_bounded_sample_with_real_sequence_and_context(
    client, db, settings, group_admin, group_reader
):
    reader, reader_id = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    base = datetime.now(UTC) - timedelta(days=3)
    for i in range(200):
        assert add_message(
            db, i, qq_id="710000001" if i % 2 else "720000002", sent_at=base + timedelta(minutes=i)
        )
    saved = snapshot(reader, group_id)
    corpus = db.get(StyleCorpus, UUID(saved["corpus"]["id"]))
    source = corpus.payload["group_sampling"]
    assert source["total_target_messages"] == 100
    assert source["sampled_anchor_messages"] <= min(
        settings.style_chat_max_windows, (settings.style_distillation_max_calls - 2) // 2
    )
    assert source["selected_messages"] <= source["sampled_anchor_messages"] * (
        settings.style_chat_context_before + settings.style_chat_context_after + 1
    )
    assert source["sampled_target_messages"] < source["total_target_messages"]
    assert source_available(db, corpus, reader_id)
    segments = corpus.payload["files"][0]["segments"]
    stored = {row.id: row for row in db.scalars(select(GroupMessage))}
    for segment in segments:
        assert segment["ordinal"] == stored[int(segment["id"].split(":")[1])].ordinal
    assert any(segment["speaker"] not in saved["target_speakers"] for segment in segments)
    assert "710000001" not in saved["target_speakers"][0]
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0


def test_revocation_cancels_pending_task_cleans_copies_and_keeps_saved_results(
    client, db, group_admin, group_reader
):
    reader, reader_id = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    assert add_message(db, 1)
    saved = snapshot(reader, group_id)
    corpus = db.get(StyleCorpus, UUID(saved["corpus"]["id"]))
    task = Task(
        user_id=reader_id,
        kind="style_distill",
        source="web",
        status="running",
        request={"kind": "personality", "corpus_id": str(corpus.id)},
    )
    db.add(task)
    db.flush()
    corpus.task_id, corpus.state = task.id, "processing"
    db.add(StyleProfile(user_id=reader_id, kind="personality", name="已保存结果"))
    db.commit()
    assert (
        client.delete(f"/api/groups/{group_id}/grants/{reader_id}", headers=HEADERS).status_code
        == 200
    )
    db.expire_all()
    assert corpus.payload == {} and corpus.state == "cleaned"
    assert corpus.preview["participants"] == []
    assert task.status == "cancelled"
    assert source_available(db, corpus, reader_id) is False
    assert db.scalar(select(func.count()).select_from(StyleProfile)) == 1
    assert reader.get(f"/api/groups/{group_id}/messages").status_code == 404


def test_clearing_group_prevents_old_event_replay_and_invalidates_snapshots(
    client, db, group_admin, group_reader
):
    reader, reader_id = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    original = event(1)
    assert ingest_group_message(db, original)
    db.commit()
    saved = snapshot(reader, group_id)
    corpus = db.get(StyleCorpus, UUID(saved["corpus"]["id"]))
    response = client.delete(f"/api/groups/{group_id}/messages", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["removed_messages"] == 1
    db.expire_all()
    assert corpus.payload == {}
    assert not source_available(db, corpus, reader_id)
    assert db.get(CollectedGroup, group_id).revision == 2
    assert ingest_group_message(db, original) is False
    db.commit()
    assert db.scalar(select(func.count()).select_from(GroupMessage)) == 0
    assert add_message(db, 2, sent_at=datetime.now(UTC) + timedelta(seconds=1)) is True


def test_rolling_expiry_erases_original_and_expired_snapshot(client, db, group_admin, group_reader):
    reader, reader_id = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    almost_expired = datetime.now(UTC) - timedelta(days=29, hours=23, minutes=59)
    assert add_message(db, 1, sent_at=almost_expired)
    saved = snapshot(reader, group_id)
    corpus = db.get(StyleCorpus, UUID(saved["corpus"]["id"]))
    assert corpus.expires_at <= almost_expired + timedelta(days=30)
    corpus.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    old = db.scalar(select(GroupMessage).where(GroupMessage.group_id == group_id))
    old.sent_at = datetime.now(UTC) - timedelta(days=31)
    db.commit()
    assert not source_available(db, corpus, reader_id)
    assert purge_expired_messages(db) == 1
    db.commit()
    assert corpus.payload == {} and corpus.cleaned_at is not None


def test_account_delete_removes_grants_and_personal_snapshots_not_shared_archive(
    client, db, group_admin, group_reader
):
    reader, reader_id = group_reader
    group_id = make_group(client)
    allow(client, group_id)
    assert add_message(db, 1)
    saved = snapshot(reader, group_id)
    response = reader.request(
        "DELETE", "/api/auth/account", json={"password": PASSWORD}, headers=HEADERS
    )
    assert response.status_code == 204, response.text
    db.expire_all()
    assert db.get(GroupGrant, (group_id, reader_id)) is None
    assert db.get(StyleCorpus, UUID(saved["corpus"]["id"])) is None
    assert db.scalar(select(func.count()).select_from(GroupMessage)) == 1
    assert db.get(CollectedGroup, group_id) is not None


def test_clearing_also_blocks_replay_of_accepted_future_dated_message(client, db, group_admin):
    group_id = make_group(client)
    future = event(100, sent_at=datetime.now(UTC) + timedelta(seconds=30))
    assert ingest_group_message(db, future)
    db.commit()
    response = client.delete(f"/api/groups/{group_id}/messages", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["removed_messages"] == 1
    assert ingest_group_message(db, future) is False
    db.commit()
    assert db.scalar(select(func.count()).select_from(GroupMessage)) == 0


def test_snapshot_rejects_excess_bytes_before_building_all_windows(
    client, db, settings, monkeypatch, group_admin
):
    group_id = make_group(client)
    for i in range(3):
        assert add_message(db, i, content="合成" * 200)
    monkeypatch.setattr(
        client.app.state, "settings", settings.model_copy(update={"style_max_upload_bytes": 1024})
    )
    response = client.post(
        f"/api/groups/{group_id}/corpus", json={"qq_id": "710000001"}, headers=HEADERS
    )
    assert response.status_code == 422
    assert "上限" in response.json()["detail"]
    assert db.scalar(select(func.count()).select_from(StyleCorpus)) == 0
