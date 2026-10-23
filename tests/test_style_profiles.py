"""Synthetic-only coverage for temporary sources and pinned style references."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from test_onboarding import HEADERS, bind, save_persona, signup

from lisa.models import (
    Account,
    ChatMessage,
    Memory,
    Persona,
    PersonaVersion,
    Project,
    StyleCorpus,
    StyleProfile,
    StyleProfileVersion,
    Task,
)
from lisa.style_corpus import clear_expired_corpora, decode_source, parse_chat, parse_writing
from lisa.style_profiles import resolve_writing_style


def upload(
    client, kind="writing", text="# 章节甲\n合成样本\n\n# 章节乙\n另一段合成样本", name="sample.md"
):
    return client.post(
        "/api/style/corpora",
        json={
            "kind": kind,
            "files": [{"name": name, "content_base64": base64.b64encode(text.encode()).decode()}],
        },
        headers=HEADERS,
    )


def profile(client, name="示例笔法", kind="writing", prompt="多用短句，描写感官细节。"):
    response = client.post(
        "/api/style/profiles",
        json={
            "kind": kind,
            "name": name,
            "prompt": prompt,
            "summary": "合成测试",
            "report": {"source_quote": "不应保存客户端证据原文"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def apply(client, version_id, kind="writing"):
    response = client.put(
        f"/api/style/applications/{kind}", json={"version_id": version_id}, headers=HEADERS
    )
    assert response.status_code == 200, response.text


def test_parsing_preserves_complete_ranges_and_file_scoped_identities():
    text = (
        "导出说明\n2026-10-23 10:00:00 小明(123)\n第一条\n继续分条\n"
        "2026-10-23 10:01:00 小红(456)\n回复"
    )
    segments, people, unknown = parse_chat(text, 0)
    assert unknown == 1
    assert len(segments) == 2
    assert "继续分条" in segments[0]["text"]
    assert segments[0]["end"] == segments[1]["start"]
    assert segments[-1]["end"] == len(text)
    assert people[0]["id"] != parse_chat(text, 1)[1][0]["id"]
    writing = "前言\n# 第一节\n甲\n第二章 转折\n乙"
    chunks = parse_writing(writing, 0)
    assert "".join(chunk["text"] for chunk in chunks) == writing
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        assert decode_source("合成测试".encode(encoding))[0] == "合成测试"


def test_upload_is_temporary_and_does_not_modify_chat_or_activity(client, db, invites):
    user_id = UUID(signup(client, invites[0])["id"])
    original_active = db.get(Account, user_id).last_active_at
    text = "2026-10-23 10:00:00 小明(123)\n仅供蒸馏的合成原文"
    response = upload(client, "personality", text, "record.txt")
    assert response.status_code == 201, response.text
    view = response.json()
    assert "仅供蒸馏" not in response.text
    assert view["preview"]["participants"][0]["messages"] == 1
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert db.scalar(select(func.count()).select_from(Memory)) == 0
    assert db.scalar(select(func.count()).select_from(Project)) == 0
    db.expire_all()
    assert db.get(Account, user_id).last_active_at == original_active
    corpus = db.get(StyleCorpus, UUID(view["id"]))
    assert text in corpus.payload["files"][0]["text"]
    response = client.delete(f"/api/style/corpora/{corpus.id}", headers=HEADERS)
    assert response.json()["cleanup_state"] == "cleaned"
    db.expire_all()
    assert corpus.payload == {}
    assert corpus.preview["participants"] == []
    assert "小明" not in str(corpus.preview)


def test_expiration_cancels_task_and_erases_payload(db):
    user = Account(username="source_test", password_hash="synthetic")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, kind="style_distill", source="web", status="running")
    db.add(task)
    db.flush()
    corpus = StyleCorpus(
        user_id=user.id,
        kind="writing",
        payload={"files": ["synthetic"]},
        preview={},
        task_id=task.id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db.add(corpus)
    db.commit()
    assert clear_expired_corpora(db) == 1
    db.commit()
    assert corpus.payload == {}
    assert task.status == "cancelled"
    assert task.error == "corpus_expired"


def test_profiles_saved_restored_and_applied_independently(client, db, invites):
    signup(client, invites[0])
    saved = profile(client, kind="personality", prompt="平和接话，适度幽默。")
    assert saved["versions"][0]["report"] == {}
    assert db.scalar(select(func.count()).select_from(Persona)) == 0
    version_one = saved["versions"][0]["id"]
    assert (
        client.put(
            "/api/style/applications/personality", json={"version_id": version_one}, headers=HEADERS
        ).status_code
        == 403
    )
    bind(client, db)
    apply(client, version_one, "personality")
    apply(client, version_one, "personality")
    assert db.scalar(select(Persona.prompt)) == "平和接话，适度幽默。"
    newer = client.post(
        f"/api/style/profiles/{saved['id']}/versions",
        json={"prompt": "安静而简短地回应。"},
        headers=HEADERS,
    ).json()
    assert newer["latest_version"] == 2
    assert client.get("/api/style/profiles").json()["applications"]["personality"] == version_one
    db.expire_all()
    assert db.scalar(select(Persona.prompt)) == "平和接话，适度幽默。"
    restored = client.post(
        f"/api/style/profiles/{saved['id']}/restore",
        json={"version_id": version_one},
        headers=HEADERS,
    )
    assert restored.json()["latest_version"] == 3
    assert client.get("/api/style/profiles").json()["applications"]["personality"] == version_one
    assert db.scalar(select(func.count()).select_from(PersonaVersion)) == 1


def test_work_override_tracks_versions_and_rejects_cross_account(client, db, invites):
    first = UUID(signup(client, invites[0])["id"])
    bind(client, db)
    save_persona(client)
    a = profile(client, "默认A")
    b = profile(client, "覆盖B", prompt="铺陈长句。")
    c = profile(client, "默认C", prompt="简洁克制。")
    va, vb, vc = [p["versions"][0]["id"] for p in (a, b, c)]
    apply(client, va)
    work_a = client.post(
        "/api/projects", json={"title": "甲", "kind": "novel"}, headers=HEADERS
    ).json()
    work_b = client.post(
        "/api/projects", json={"title": "乙", "kind": "novel"}, headers=HEADERS
    ).json()
    response = client.put(
        f"/api/projects/{work_b['id']}/writing-style", json={"version_id": vb}, headers=HEADERS
    )
    assert response.json()["effective_version_id"] == vb
    apply(client, vc)
    assert resolve_writing_style(db, first, UUID(work_a["id"]))["effective_version_id"] == vc
    assert resolve_writing_style(db, first, UUID(work_b["id"]))["effective_version_id"] == vb
    response = client.put(
        f"/api/projects/{work_b['id']}/writing-style", json={"version_id": None}, headers=HEADERS
    )
    assert response.json()["effective_version_id"] == vc
    client.post("/api/auth/logout", headers=HEADERS)
    signup(client, invites[1], "reader_two")
    assert (
        client.put(
            "/api/style/applications/writing", json={"version_id": vb}, headers=HEADERS
        ).status_code
        == 404
    )
    assert client.get(f"/api/projects/{work_a['id']}/writing-style").status_code == 404
    assert client.get("/api/style/profiles").json()["profiles"] == []


def test_unsupported_or_invalid_material_is_rejected(client, invites):
    signup(client, invites[0])
    assert upload(client, name="book.pdf").status_code == 422
    assert upload(client, text="   ").status_code == 422
    assert (
        client.post(
            "/api/style/corpora",
            json={
                "kind": "writing",
                "files": [{"name": "broken.txt", "content_base64": "invalid!!"}],
            },
            headers=HEADERS,
        ).status_code
        == 422
    )


def test_account_deletion_removes_temporary_sources_and_profiles(client, db, invites):
    signup(client, invites[0])
    upload(client)
    saved = profile(client)
    apply(client, saved["versions"][0]["id"])
    response = client.request(
        "DELETE", "/api/auth/account", json={"password": "synthetic-password-2026"}, headers=HEADERS
    )
    assert response.status_code == 204, response.text
    db.expire_all()
    for model in (StyleCorpus, StyleProfile, StyleProfileVersion):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_editing_a_candidate_does_not_claim_its_previous_validation(client, db, invites):
    user_id = UUID(signup(client, invites[0])["id"])
    task = Task(
        user_id=user_id,
        kind="style_distill",
        source="web",
        status="completed",
        request={"kind": "writing"},
        result={
            "prompt": "原候选指引。",
            "report": {"validation": {"status": "model_reviewed"}, "coverage": {"files": []}},
        },
    )
    db.add(task)
    db.commit()
    response = client.post(
        "/api/style/profiles",
        json={
            "kind": "writing",
            "name": "编辑后候选",
            "prompt": "用户修改后的指引。",
            "source_task_id": str(task.id),
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    report = saved["versions"][0]["report"]
    assert report["validation"]["status"] == "edited_not_evaluated"
    assert report["validation"]["original_candidate_validation"]["status"] == "model_reviewed"
    assert report["edited_from_source"] is True
    original = client.post(
        "/api/style/profiles",
        json={
            "kind": "writing",
            "name": "原候选",
            "prompt": "原候选指引。",
            "source_task_id": str(task.id),
        },
        headers=HEADERS,
    ).json()
    assert original["versions"][0]["report"]["validation"]["status"] == "model_reviewed"
    updated = client.post(
        f"/api/style/profiles/{original['id']}/versions",
        json={"prompt": "再次编辑的指引。"},
        headers=HEADERS,
    ).json()
    assert updated["versions"][0]["report"]["validation"]["status"] == "edited_not_evaluated"
