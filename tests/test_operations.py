"""Real isolated database coverage for the private operations read model."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from lisa.app import create_app
from lisa.models import (
    Account,
    ActivityPlan,
    BudgetDay,
    Memory,
    ModelCall,
    Project,
    RagEvaluationRun,
    RetrievalObservation,
    RuntimeHeartbeat,
    Task,
)
from lisa.operations import period
from lisa.runtime_status import cleanup_observations, heartbeat

HEADERS = {"X-LISA-Request": "1"}


def operator(client, invites):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "operator",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"operations_maintainers": "operator"}
    )
    return UUID(response.json()["id"])


def test_maintenance_allowlist_does_not_inherit_old_admin(client, db, invites):
    assert client.get("/api/operations/dashboard").status_code == 401
    uid = operator(client, invites)
    client.app.state.settings.operations_maintainers = ""
    user = db.get(Account, uid)
    user.is_admin = True
    db.commit()
    assert client.get("/api/operations/access").json() == {"allowed": False}
    assert client.get("/api/operations/dashboard").status_code == 403
    client.app.state.settings.operations_maintainers = str(uid)
    assert client.get("/api/operations/access").json() == {"allowed": True}


def test_empty_dashboard_has_null_rates_and_no_private_bodies(client, invites):
    operator(client, invites)
    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["calls"]["total"] == 0
    assert data["calls"]["success_rate"] is None
    assert data["tasks"]["success_rate"] is None
    assert data["rag"]["evaluation"] is None
    assert data["costs"]["currency"] == "CNY"
    assert data["queue"]["pending"] == 0
    assert response.headers["Cache-Control"] == "no-store"


def test_period_is_local_calendar_not_old_utc_budget_day(settings):
    start, end = period(settings, "today", now=datetime(2026, 10, 28, 18, tzinfo=UTC))
    assert start == datetime(2026, 10, 28, 16, tzinfo=UTC)
    assert end == datetime(2026, 10, 29, 16, tzinfo=UTC)
    start, end = period(settings, "custom", date(2026, 10, 28), date(2026, 10, 29))
    assert end - start == timedelta(days=2)
    with pytest.raises(HTTPException):
        period(settings, "custom", date(2026, 1, 1), date(2026, 12, 31))


def test_model_costs_and_dispatch_are_not_conflated(client, db, invites):
    uid = operator(client, invites)
    now = datetime.now(UTC)
    db.add(BudgetDay(day=now.date(), spent_usd=999))
    task = Task(
        user_id=uid,
        kind="chat",
        status="completed",
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=2),
        request={"private": "do-not-expose"},
    )
    db.add(task)
    db.flush()
    for conf, amount, status, tokens in (
        (
            {
                "purpose": "chat",
                "model": "synthetic",
                "dispatch_state": "sent",
                "price_known": True,
                "cost_source": "estimate",
            },
            "0.2",
            "succeeded",
            10,
        ),
        (
            {
                "purpose": "image_generation",
                "model": "image",
                "dispatch_state": "sent",
                "price_known": False,
                "cost_source": "unknown",
            },
            "0",
            "succeeded",
            0,
        ),
        (
            {
                "purpose": "query_embedding",
                "model": "embed",
                "dispatch_state": "unknown",
                "price_known": True,
                "cost_source": "conservative",
            },
            "0.3",
            "unknown",
            None,
        ),
        ({"model": "old"}, "0", "succeeded", 0),
    ):
        db.add(
            ModelCall(
                task_id=task.id,
                user_id=uid,
                budget_day=now.date(),
                state=status,
                reserved_usd=0,
                charged_usd=Decimal(amount),
                input_tokens=tokens,
                output_tokens=tokens,
                configuration=conf,
                started_at=now,
                finished_at=now + timedelta(seconds=1),
            )
        )
    db.commit()
    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["calls"]["total"] == 4
    assert data["calls"]["confirmed_sent"] == 2
    assert data["calls"]["unknown_dispatch"] == 2
    assert data["costs"]["known_total"] == 0.5
    assert data["costs"]["estimate"] == 0.2
    assert data["costs"]["conservative"] == 0.3
    assert data["costs"]["unknown_count"] == 2
    assert data["costs"]["incomplete"] is True
    assert "do-not-expose" not in response.text
    assert all(
        r["amount"] is None for r in data["calls"]["recent"] if r["cost_source"] == "unknown"
    )
    assert client.get("/api/operations/dashboard?purpose=chat").json()["calls"]["total"] == 1
    assert any(x["code"] == "unknown_outcome" for x in data["alerts"])


def test_queue_batches_and_stale_components_are_distinct(client, db, invites):
    uid = operator(client, invites)
    now = datetime.now(UTC)
    db.add_all(
        [
            Task(
                user_id=uid,
                kind="chat",
                status="pending",
                created_at=now,
                request={"chat_silence_until": (now + timedelta(seconds=10)).isoformat()},
            ),
            Task(
                user_id=uid, kind="scene", status="pending", created_at=now - timedelta(minutes=8)
            ),
            RuntimeHeartbeat(
                component="worker", status="ok", observed_at=now - timedelta(minutes=5)
            ),
        ]
    )
    db.commit()
    data = client.get("/api/operations/dashboard").json()
    assert data["queue"]["debouncing"] == 1
    assert data["queue"]["pending"] == 1
    assert data["queue"]["oldest_wait_seconds"] >= 480
    assert next(c for c in data["components"] if c["component"] == "worker")["status"] == "stale"
    assert next(c for c in data["components"] if c["component"] == "qq")["status"] == "unknown"


def test_rag_observations_and_never_indexed_sources(client, db, invites):
    uid = operator(client, invites)
    task = Task(user_id=uid, kind="chat", status="completed")
    db.add(task)
    db.flush()
    db.add(Memory(user_id=uid, kind="fact", content="private secret memory", allow_chat=True))
    db.add(
        RetrievalObservation(
            task_id=task.id,
            user_id=uid,
            purpose="chat",
            metrics={
                "state": "executed",
                "route": "semantic",
                "candidates": 3,
                "assembled": 2,
                "final_count": 1,
                "final_chars": 100,
                "durations_ms": {"total": 12},
                "omitted": {"request_budget": 1},
                "candidate_keys": ["private-source-id"],
            },
        )
    )
    db.add(
        RagEvaluationRun(
            configuration={"dataset_version": "synthetic-v1"},
            results={"sample_count": 36, "evaluation_kind": "deterministic_pipeline"},
        )
    )
    db.commit()
    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rag"]["observations"] == 1
    assert data["rag"]["counts"]["final_count"] == 1
    assert data["rag"]["omitted"] == [{"name": "request_budget", "count": 1}]
    chat = next(c for c in data["rag"]["coverage"] if c["purpose"] == "chat")
    assert chat["total"] == 1 and chat["missing"] == 1
    assert "private secret memory" not in response.text
    assert "private-source-id" not in response.text
    assert data["rag"]["evaluation"]["results"]["sample_count"] == 36
    db.execute(delete(Account).where(Account.id == uid))
    db.commit()
    assert db.scalar(select(RetrievalObservation.id)) is None


def test_monitor_failure_is_best_effort():
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("synthetic database outage")

    assert heartbeat(BrokenEngine(), "worker") is False


def test_napcat_is_independent_from_gateway_and_has_a_clean_maintainer_link(client, db, invites):
    operator(client, invites)
    client.app.state.settings = client.app.state.settings.model_copy(update={
        "napcat_webui_url": "https://console.example.test/webui",
        "napcat_webui_probe_url": "http://127.0.0.1:6099/webui",
    })
    db.add_all([
        RuntimeHeartbeat(component="napcat", status="ok", details={
            "process_running": True, "webui_reachable": False,
        }),
        RuntimeHeartbeat(component="gateway", status="offline"),
        RuntimeHeartbeat(component="qq", status="offline"),
    ])
    db.commit()
    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200, response.text
    components = {row["component"]: row for row in response.json()["components"]}
    assert components["napcat"]["status"] == "ok"
    assert components["gateway"]["status"] == components["qq"]["status"] == "offline"
    assert components["napcat"]["details"] == {
        "process_running": True, "webui_reachable": False,
        "webui_url": "https://console.example.test/webui",
        "open_url": "/api/operations/napcat/open",
    }
    assert "127.0.0.1:6099" not in response.text
    client.app.state.settings.operations_maintainers = ""
    denied = client.get("/api/operations/dashboard")
    assert denied.status_code == 403
    assert "console.example.test" not in denied.text


def test_napcat_auto_login_is_on_demand_maintainer_only_and_not_logged(client, invites, caplog):
    token = "synthetic-private-token+/?="
    client.app.state.settings = client.app.state.settings.model_copy(update={
        "napcat_webui_url": "https://napcat.example.test/webui",
        "napcat_webui_probe_url": "https://napcat.example.test/webui",
        "napcat_webui_token": SecretStr(token),
    })
    assert client.get("/api/operations/napcat/open", follow_redirects=False).status_code == 401
    operator(client, invites)
    with caplog.at_level("INFO", logger="lisa.api"):
        status = client.get("/api/operations/dashboard")
        assert token not in status.text and "token=" not in status.text
        response = client.get(
            "/api/operations/napcat/open?url=https://other.example.test", follow_redirects=False
        )
    assert response.status_code == 303
    destination = urlsplit(response.headers["location"])
    assert destination.netloc == "napcat.example.test"
    assert parse_qs(destination.query) == {"token": [token]}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert token not in caplog.text and "synthetic-private-token" not in caplog.text
    client.app.state.settings.operations_maintainers = ""
    denied = client.get("/api/operations/napcat/open", follow_redirects=False)
    assert denied.status_code == 403 and "location" not in denied.headers


def test_eligible_wait_excludes_batching_and_includes_overdue_unqueued_plan(client, db, invites):
    uid = operator(client, invites)
    now = datetime.now(UTC)
    project = Project(user_id=uid, title="synthetic plan")
    db.add(project)
    db.flush()
    db.add_all(
        [
            Task(
                user_id=uid,
                kind="chat",
                status="pending",
                created_at=now - timedelta(minutes=10),
                request={"chat_silence_until": (now - timedelta(seconds=2)).isoformat()},
            ),
            ActivityPlan(
                user_id=uid,
                project_id=project.id,
                goal="synthetic",
                status="pending",
                due_at=now - timedelta(seconds=5),
                activity_at=now,
            ),
            RuntimeHeartbeat(
                component="qq",
                status="ok",
                observed_at=now,
                details={"connected": True, "logged_in": False},
            ),
        ]
    )
    db.commit()
    response = client.get("/api/operations/dashboard")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["queue"]["pending"] == 2
    assert 5 <= data["queue"]["oldest_wait_seconds"] < 30
    assert next(c for c in data["components"] if c["component"] == "qq")["status"] == "offline"


def test_observation_retention_removes_only_expired_telemetry(db, engine):
    old = RagEvaluationRun(created_at=datetime.now(UTC) - timedelta(days=31))
    fresh = RagEvaluationRun()
    db.add_all([old, fresh])
    db.commit()
    old_id, fresh_id = old.id, fresh.id
    cleanup_observations(engine, 30)
    db.expire_all()
    assert db.scalar(select(RagEvaluationRun.id).where(RagEvaluationRun.id == old_id)) is None
    assert db.get(RagEvaluationRun, fresh_id) is not None


def test_high_legacy_daily_ledger_does_not_disable_text_service(client, db, invites):
    operator(client, invites)
    settings = client.app.state.settings.model_copy(
        update={
            "chat_enabled": True,
            "model_api_key": SecretStr("synthetic-test-key"),
        }
    )
    db.add(BudgetDay(day=datetime.now(UTC).date(), spent_usd=9999))
    db.commit()
    with TestClient(create_app(settings)) as configured:
        login = configured.post(
            "/api/auth/login",
            headers=HEADERS,
            json={
                "username": "operator",
                "password": "synthetic-password-2026",
            },
        )
        assert login.status_code == 200
        assert configured.get("/api/service-status").json()["status"] == "ready"
