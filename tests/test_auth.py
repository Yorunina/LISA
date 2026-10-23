from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from lisa.app import create_app
from lisa.auth import issue_invites
from lisa.models import Account, Invite, LoginSession
from lisa.security import hash_token, verify_password

HEADERS = {"X-LISA-Request": "1"}
PASSWORD = "synthetic-password-2026"


def register(client, invite, username="reader_one"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD, "invite_code": invite},
        headers=HEADERS,
    )


def test_registration_persists_only_hashes(client, db, invites):
    response = register(client, invites[0], "Reader_One")
    assert response.status_code == 201, response.text
    assert response.json()["username"] == "reader_one"
    assert set(response.json()) == {"id", "username"}
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/" in cookie
    account = db.scalar(select(Account))
    assert account.password_hash != PASSWORD
    assert verify_password(PASSWORD, account.password_hash)
    invite = db.get(Invite, hash_token(invites[0]))
    assert invite is not None and invite.used_by == account.id
    session = db.scalar(select(LoginSession))
    assert session.user_id == account.id
    assert session.token_hash == hash_token(next(iter(client.cookies.values())))
    assert client.get("/api/auth/me").json() == response.json()


def test_invalid_invite_creates_nothing(client, db):
    assert register(client, "not-an-invite").status_code == 400
    assert db.scalar(select(func.count()).select_from(Account)) == 0
    assert db.scalar(select(func.count()).select_from(LoginSession)) == 0


def test_consumed_invite_cannot_be_reused(client, db, invites):
    assert register(client, invites[0]).status_code == 201
    assert register(client, invites[0], "another_reader").status_code == 400
    assert db.scalar(select(func.count()).select_from(Account)) == 1


def test_duplicate_username_rolls_back_invite_consumption(client, db, invites):
    assert register(client, invites[0], "reader_one").status_code == 201
    assert register(client, invites[1], "READER_ONE").status_code == 409
    assert db.get(Invite, hash_token(invites[1])).used_by is None
    assert register(client, invites[1], "reader_two").status_code == 201


def test_concurrent_invite_reuse_has_one_winner(settings, db, invites):
    barrier = Barrier(2)

    def attempt(username):
        with TestClient(create_app(settings)) as concurrent_client:
            barrier.wait(timeout=10)
            return register(concurrent_client, invites[0], username).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, ["reader_one", "reader_two"]))
    assert sorted(statuses) == [201, 400]
    assert db.scalar(select(func.count()).select_from(Account)) == 1
    assert db.scalar(select(func.count()).select_from(LoginSession)) == 1


@pytest.mark.parametrize(
    ("username", "password"),
    [("ab", PASSWORD), ("not valid", PASSWORD), ("读者一", PASSWORD), ("reader", "short")],
)
def test_invalid_registration_does_not_consume_invite(client, db, invites, username, password):
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invites[0]},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert db.get(Invite, hash_token(invites[0])).used_by is None


def test_login_logout_revokes_session(client, db, invites):
    identity = register(client, invites[0]).json()
    old_cookies = dict(client.cookies)
    assert client.post("/api/auth/logout", headers=HEADERS).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert db.scalar(select(func.count()).select_from(LoginSession)) == 0
    client.cookies.update(old_cookies)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    login = client.post(
        "/api/auth/login",
        json={"username": "READER_ONE", "password": PASSWORD},
        headers=HEADERS,
    )
    assert login.status_code == 200
    assert login.json() == identity
    assert client.get("/api/auth/me").json() == identity


def test_invalid_credentials_are_indistinguishable(client, invites):
    register(client, invites[0])
    client.cookies.clear()
    responses = [
        client.post(
            "/api/auth/login", json={"username": username, "password": password}, headers=HEADERS
        )
        for username, password in [("reader_one", "wrong-password"), ("missing", PASSWORD)]
    ]
    assert [r.status_code for r in responses] == [401, 401]
    assert responses[0].json() == responses[1].json()
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(client, db, invites):
    register(client, invites[0])
    db.execute(update(LoginSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_account_cannot_use_session_or_login(client, db, invites):
    register(client, invites[0])
    db.execute(update(Account).values(active=False))
    db.commit()
    assert client.get("/api/auth/me").status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "reader_one", "password": PASSWORD},
        headers=HEADERS,
    ).status_code == 401


def test_two_users_identity_comes_only_from_session(client, settings, invites):
    first = register(client, invites[0], "reader_one").json()
    with TestClient(create_app(settings)) as other:
        second = register(other, invites[1], "reader_two").json()
        assert first["id"] != second["id"]
        assert other.get("/api/auth/me").json() == second
        spoof = client.get(
            "/api/auth/me", params={"user_id": second["id"]}, headers={"X-User-ID": second["id"]}
        )
        assert spoof.json() == first
        client.post("/api/auth/logout", headers=HEADERS)
        assert other.get("/api/auth/me").json() == second


def test_mutations_require_same_origin_header(client, invites):
    payload = {"username": "reader_one", "password": PASSWORD, "invite_code": invites[0]}
    assert client.post("/api/auth/register", json=payload).status_code == 403
    assert client.post("/api/auth/login", json=payload).status_code == 403
    assert client.post("/api/auth/logout").status_code == 403
    assert register(client, invites[0]).status_code == 201
    preflight = client.options(
        "/api/auth/logout",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-LISA-Request",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers


def test_invitation_limit_is_twenty_total_seats(db):
    assert len(issue_invites(db, 20)) == 20
    db.commit()
    with pytest.raises(ValueError):
        issue_invites(db, 1)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Invite)) == 20
