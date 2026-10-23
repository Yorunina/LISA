from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lisa.app import create_app
from lisa.models import (
    Account,
    GuideNotice,
    Persona,
    PersonaVersion,
    QQBinding,
    ServiceCode,
    ServiceEvent,
)
from lisa.onboarding import handle_service_message, issue_code
from lisa.security import hash_token

HEADERS = {"X-LISA-Request": "1"}
PASSWORD = "synthetic-password-2026"
QQ = "700000001"


def signup(client, invite, username="reader_one"):
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD, "invite_code": invite},
        headers=HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def code_for(db, purpose="binding", qq_id=QQ, user_id=None):
    code = issue_code(db, purpose, qq_id, user_id)
    db.commit()
    return code


def bind(client, db, qq_id=QQ):
    code = code_for(db, qq_id=qq_id)
    response = client.post(
        "/api/setup/binding/confirm", json={"code": code, "qq_id": qq_id}, headers=HEADERS
    )
    assert response.status_code == 200
    return code


def save_persona(client, *, name="小莎", prompt="合成测试：以平和的语气交流。"):
    return client.post("/api/setup/persona", json={"name": name, "prompt": prompt}, headers=HEADERS)


def service(db, event_id, text="你好", qq_id=QQ):
    result = handle_service_message(
        db,
        bot_id="900000001",
        message_id=str(event_id),
        qq_id=qq_id,
        sent_at=datetime.now(UTC),
        text=text,
        web_url="http://localhost:8000",
    )
    db.commit()
    return result


def test_binding_preview_requires_confirmation_and_matching_qq(client, db, invites):
    assert client.get("/api/setup").status_code == 401
    signup(client, invites[0])
    assert client.get("/api/setup").json() == {
        "qq_id": None,
        "initialized": False,
        "persona": None,
        "presets": [],
    }
    code = code_for(db)
    preview = client.post("/api/setup/binding/preview", json={"code": code}, headers=HEADERS)
    assert preview.status_code == 200
    assert preview.json()["qq_id"] == QQ
    assert db.scalar(select(func.count()).select_from(QQBinding)) == 0
    assert db.get(ServiceCode, hash_token(code)).used_at is None
    assert (
        client.post(
            "/api/setup/binding/confirm",
            json={"code": code, "qq_id": "700000009"},
            headers=HEADERS,
        ).status_code
        == 400
    )
    assert db.get(ServiceCode, hash_token(code)).used_at is None
    response = client.post(
        "/api/setup/binding/confirm", json={"code": code, "qq_id": QQ}, headers=HEADERS
    )
    assert response.json()["qq_id"] == QQ
    assert response.json()["initialized"] is False
    assert (
        client.post(
            "/api/setup/binding/confirm", json={"code": code, "qq_id": QQ}, headers=HEADERS
        ).status_code
        == 400
    )


@pytest.mark.parametrize("purpose", ["binding", "recovery", "reset"])
def test_expired_service_codes_are_rejected(client, db, invites, purpose):
    user = signup(client, invites[0])
    bind(client, db)
    code = code_for(db, purpose, user_id=UUID(user["id"]))
    record = db.get(ServiceCode, hash_token(code))
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    path, body = {
        "binding": ("/api/setup/binding/confirm", {"code": code, "qq_id": QQ}),
        "recovery": ("/api/auth/recovery/verify", {"code": code}),
        "reset": ("/api/auth/recovery/reset", {"reset_token": code, "password": PASSWORD}),
    }[purpose]
    assert client.post(path, json=body, headers=HEADERS).status_code == 400
    assert db.get(ServiceCode, hash_token(code)).used_at is None


def test_binding_recovery_and_reset_codes_are_not_interchangeable(client, db, invites):
    user = signup(client, invites[0])
    binding = code_for(db)
    recovery = code_for(db, "recovery", user_id=UUID(user["id"]))
    reset = code_for(db, "reset", user_id=UUID(user["id"]))
    for token in (recovery, reset):
        assert (
            client.post(
                "/api/setup/binding/preview", json={"code": token}, headers=HEADERS
            ).status_code
            == 400
        )
    for token in (binding, reset):
        assert (
            client.post(
                "/api/auth/recovery/verify", json={"code": token}, headers=HEADERS
            ).status_code
            == 400
        )
    for token in (binding, recovery):
        assert (
            client.post(
                "/api/auth/recovery/reset",
                json={"reset_token": token, "password": PASSWORD},
                headers=HEADERS,
            ).status_code
            == 400
        )
    assert (
        db.scalar(
            select(func.count()).select_from(ServiceCode).where(ServiceCode.used_at.is_not(None))
        )
        == 0
    )


def test_binding_conflicts_do_not_replace_or_merge_accounts(client, settings, db, invites):
    first = signup(client, invites[0])
    bind(client, db)
    another_qq_code = code_for(db, qq_id="700000002")
    assert (
        client.post(
            "/api/setup/binding/preview", json={"code": another_qq_code}, headers=HEADERS
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/setup/binding/confirm",
            json={"code": another_qq_code, "qq_id": "700000002"},
            headers=HEADERS,
        ).status_code
        == 409
    )
    with TestClient(create_app(settings)) as other:
        second = signup(other, invites[1], "reader_two")
        occupied_code = code_for(db)
        assert (
            other.post(
                "/api/setup/binding/preview", json={"code": occupied_code}, headers=HEADERS
            ).status_code
            == 409
        )
        assert (
            other.post(
                "/api/setup/binding/confirm",
                json={"code": occupied_code, "qq_id": QQ},
                headers=HEADERS,
            ).status_code
            == 409
        )
        assert other.get("/api/setup").json()["qq_id"] is None
    assert db.get(QQBinding, UUID(first["id"])).qq_id == QQ
    assert db.get(QQBinding, UUID(second["id"])) is None
    assert db.get(ServiceCode, hash_token(occupied_code)).used_at is None


def test_persona_versions_noop_and_user_isolation(client, settings, db, invites):
    first = signup(client, invites[0])
    assert save_persona(client).status_code == 403
    bind(client, db)
    assert save_persona(client, prompt="   ").status_code == 422
    assert client.get("/api/setup/presets").json() == []
    initial = save_persona(client).json()
    assert initial["initialized"] is True
    assert initial["persona"]["version"] == 1
    persona_id = db.scalar(select(Persona.id))
    assert save_persona(client).json() == initial
    changed = save_persona(client, prompt="合成测试：语气简洁，但保留共同经历。")
    assert changed.json()["persona"]["version"] == 2
    assert db.scalar(select(Persona.id)) == persona_id
    assert db.scalar(select(func.count()).select_from(PersonaVersion)) == 2
    with TestClient(create_app(settings)) as other:
        signup(other, invites[1], "reader_two")
        assert other.get("/api/setup", params={"user_id": first["id"]}).json()["persona"] is None
        bind(other, db, "700000002")
        assert (
            save_persona(other, name="另一个小莎", prompt="另一个用户的合成设定。").status_code
            == 200
        )
    assert client.get("/api/setup").json() == changed.json()


def test_recovery_revokes_sessions_and_preserves_persona(client, settings, db, invites):
    first = signup(client, invites[0])
    bind(client, db)
    expected_setup = save_persona(client).json()
    persona_id = db.scalar(select(Persona.id))
    recovery = code_for(db, "recovery", user_id=UUID(first["id"]))
    with TestClient(create_app(settings)) as other:
        second = signup(other, invites[1], "reader_two")
        result = other.post("/api/auth/recovery/verify", json={"code": recovery}, headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["username"] == first["username"]
        assert (
            other.post(
                "/api/auth/recovery/verify", json={"code": recovery}, headers=HEADERS
            ).status_code
            == 400
        )
        ticket = result.json()["reset_token"]
        assert (
            other.post(
                "/api/auth/recovery/reset",
                json={"reset_token": ticket, "password": "short"},
                headers=HEADERS,
            ).status_code
            == 422
        )
        reset_body = {"reset_token": ticket, "password": "replacement-password-2026"}
        assert (
            other.post("/api/auth/recovery/reset", json=reset_body, headers=HEADERS).status_code
            == 204
        )
        assert (
            other.post("/api/auth/recovery/reset", json=reset_body, headers=HEADERS).status_code
            == 400
        )
        assert other.get("/api/auth/me").json() == second
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": first["username"], "password": PASSWORD},
            headers=HEADERS,
        ).status_code
        == 401
    )
    login = client.post(
        "/api/auth/login",
        json={"username": first["username"], "password": reset_body["password"]},
        headers=HEADERS,
    )
    assert login.json() == first
    assert client.get("/api/setup").json() == expected_setup
    assert db.scalar(select(Persona.id)) == persona_id
    assert db.scalar(select(func.count()).select_from(PersonaVersion)) == 1


def test_disabled_account_cannot_be_recovered(client, db, invites):
    first = signup(client, invites[0])
    bind(client, db)
    code = code_for(db, "recovery", user_id=UUID(first["id"]))
    user = db.get(Account, UUID(first["id"]))
    user.active = False
    db.commit()
    assert (
        client.post("/api/auth/recovery/verify", json={"code": code}, headers=HEADERS).status_code
        == 400
    )


def test_guidance_is_throttled_and_replayed_events_are_ignored(client, db, invites):
    first_event, first_reply = service(db, 1)
    assert "注册并绑定 QQ" in first_reply
    assert service(db, 2)[1] is None
    assert service(db, 1)[1] is None
    assert db.get(ServiceEvent, first_event).sent_at.tzinfo is not None
    notice = db.get(GuideNotice, QQ)
    notice.sent_at = datetime.now(UTC) - timedelta(minutes=6)
    db.commit()
    assert service(db, 3)[1] is not None
    signup(client, invites[0])
    bind(client, db)
    assert "完成性格选择" in service(db, 4)[1]
    assert service(db, 5)[1] is None
    assert save_persona(client).status_code == 200
    event_id, reply = service(db, 6)
    assert reply is None
    assert db.get(ServiceEvent, event_id).status == "ignored"
    assert service(db, 3)[1] is None


def test_service_commands_issue_private_codes_and_reissue_invalidates_previous(db):
    event_id, reply = service(db, 1, "绑定")
    first_code = reply.splitlines()[0].split("：", 1)[1]
    assert db.get(ServiceCode, hash_token(first_code)).qq_id == QQ
    assert service(db, 1, "绑定") == (event_id, None)
    assert db.scalar(select(func.count()).select_from(ServiceCode)) == 1
    second_reply = service(db, 2, "绑定")[1]
    second_code = second_reply.splitlines()[0].split("：", 1)[1]
    assert first_code != second_code
    assert db.get(ServiceCode, hash_token(first_code)) is None
    assert db.get(ServiceCode, hash_token(second_code)).qq_id == QQ
    assert "尚未绑定" in service(db, 3, "找回")[1]
