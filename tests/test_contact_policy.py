from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_works import HEADERS
from test_works import owner as owner

from lisa.contact_policy import contact_block_reason, contact_snapshot
from lisa.models import (
    Account,
    ChatMessage,
    ContactPreference,
    OutboundMessage,
    Persona,
    QQBinding,
    Task,
)


@pytest.fixture
def contact_context(db, settings):
    now = datetime(2026, 10, 23, 4, tzinfo=UTC)
    user = Account(
        username="contact_policy",
        password_hash="synthetic",
        last_active_at=now - timedelta(hours=6),
    )
    db.add(user)
    db.flush()
    db.add_all(
        [
            QQBinding(user_id=user.id, qq_id="710006001"),
            Persona(user_id=user.id, name="LISA", prompt="合成测试"),
            ContactPreference(user_id=user.id, enabled=True),
        ]
    )
    chat = Task(user_id=user.id, kind="chat", source="qq", status="completed")
    db.add(chat)
    db.flush()
    message = ChatMessage(
        user_id=user.id,
        task_id=chat.id,
        role="user",
        content="合成反馈",
        sent_at=user.last_active_at,
        created_at=user.last_active_at,
    )
    db.add(message)
    db.commit()
    return user, chat, message, now, settings.model_copy(update={"proactive_enabled": True})


def block(db, context, **kwargs):
    user, _, _, now, settings = context
    return contact_block_reason(db, settings, user.id, now=now, **kwargs)


def attempt(db, context, *, hours=5, status="failed", same_activity=False):
    user, _, _, now, _ = context
    snapshot = contact_snapshot(db, user.id)
    if not same_activity:
        snapshot["activity_at"] = (user.last_active_at - timedelta(days=1)).isoformat()
    task = Task(
        user_id=user.id,
        kind="contact",
        source="autonomous",
        status=status,
        request=snapshot,
        created_at=now - timedelta(hours=hours),
    )
    db.add(task)
    db.flush()
    return task


def test_settings_defaults_are_read_only_and_save_is_explicit(client, db, owner):
    user = db.get(Account, owner)
    before = datetime(2026, 10, 22, tzinfo=UTC)
    user.last_active_at = before
    db.commit()
    response = client.get("/api/contact-settings")
    assert response.status_code == 200
    data = response.json()
    assert data.pop("observations")["sample_count"] == 0
    assert data == {
        "enabled": False,
        "timezone": "Asia/Shanghai",
        "quiet_start": None,
        "quiet_end": None,
        "min_interval_minutes": 240,
        "max_per_day": 2,
        "revision": 1,
        "available": False,
    }
    assert db.get(ContactPreference, owner) is None
    values = {"enabled": True, "quiet_start": 1320, "quiet_end": 480}
    saved = client.post("/api/contact-settings", json=values, headers=HEADERS)
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert (
        client.post("/api/contact-settings", json=values, headers=HEADERS).json()["revision"] == 1
    )
    values["min_interval_minutes"] = 300
    assert (
        client.post("/api/contact-settings", json=values, headers=HEADERS).json()["revision"] == 2
    )
    db.expire_all()
    assert db.get(Account, owner).last_active_at == before
    assert db.get(Account, owner).memory_revision == 1


def test_settings_require_authentication(client):
    assert client.get("/api/contact-settings").status_code == 401
    assert client.post("/api/contact-settings", json={}, headers=HEADERS).status_code == 401


def test_settings_are_scoped_to_login(client, db, owner):
    other = Account(username="other_contact", password_hash="synthetic")
    db.add(other)
    db.flush()
    db.add(ContactPreference(user_id=other.id, enabled=True, timezone="Europe/London"))
    db.commit()
    assert client.get("/api/contact-settings").json()["enabled"] is False
    assert (
        client.post(
            "/api/contact-settings", json={"timezone": "Asia/Tokyo"}, headers=HEADERS
        ).status_code
        == 200
    )
    db.expire_all()
    assert db.get(ContactPreference, other.id).timezone == "Europe/London"


@pytest.mark.parametrize(
    "values",
    [
        {"timezone": "Missing/Zone"},
        {"quiet_start": 100},
        {"quiet_end": 100},
        {"quiet_start": 100, "quiet_end": 100},
        {"quiet_start": -1, "quiet_end": 100},
        {"quiet_start": 100, "quiet_end": 1440},
        {"min_interval_minutes": 59},
        {"min_interval_minutes": 1441},
        {"max_per_day": 0},
        {"max_per_day": 6},
    ],
)
def test_invalid_contact_settings_rejected(client, owner, values):
    assert client.post("/api/contact-settings", json=values, headers=HEADERS).status_code == 422


def test_eligible_snapshot_excludes_own_attempt(db, contact_context):
    assert block(db, contact_context) is None
    current = attempt(db, contact_context, hours=0, same_activity=True, status="running")
    assert block(db, contact_context, task=current) is None
    assert set(current.request) == {
        "activity_at",
        "memory_revision",
        "preferences_revision",
        "persona_version",
    }


@pytest.mark.parametrize(
    "change,reason",
    [
        ("global", "disabled"),
        ("user", "user_disabled"),
        ("binding", "account_unavailable"),
        ("persona", "account_unavailable"),
        ("inactive", "inactive"),
        ("no_activity", "inactive"),
        ("pending", "unprocessed_chat"),
        ("running", "unprocessed_chat"),
        ("outbound", "unprocessed_chat"),
    ],
)
def test_ineligible_accounts(db, contact_context, change, reason):
    user, chat, message, now, settings = contact_context
    if change == "global":
        settings.proactive_enabled = False
    elif change == "user":
        db.get(ContactPreference, user.id).enabled = False
    elif change == "binding":
        db.delete(db.get(QQBinding, user.id))
    elif change == "persona":
        db.delete(db.scalar(select(Persona).where(Persona.user_id == user.id)))
    elif change == "inactive":
        user.last_active_at = now - timedelta(hours=24)
    elif change == "no_activity":
        user.last_active_at = None
    elif change in ("pending", "running"):
        chat.status = change
    else:
        db.add(
            OutboundMessage(
                user_id=user.id,
                task_id=chat.id,
                qq_id="710006001",
                content="合成回复",
                status="pending",
            )
        )
    db.flush()
    assert block(db, contact_context) == reason


@pytest.mark.parametrize(
    "local_hour,quiet", [(21, False), (22, True), (0, True), (7, True), (8, False)]
)
def test_cross_midnight_quiet_boundaries(db, contact_context, local_hour, quiet):
    user, _, _, _, settings = contact_context
    now = datetime(2026, 10, 23, local_hour, tzinfo=UTC) - timedelta(hours=8)
    user.last_active_at = now - timedelta(hours=6)
    preference = db.get(ContactPreference, user.id)
    preference.quiet_start, preference.quiet_end = 1320, 480
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) == (
        "quiet_hours" if quiet else None
    )


def test_quiet_hours_follow_configured_timezone(db, contact_context):
    user, _, _, _, _ = contact_context
    preference = db.get(ContactPreference, user.id)
    preference.timezone = "America/New_York"
    preference.quiet_start, preference.quiet_end = 0, 60
    db.flush()
    # 04:00 UTC is midnight in New York on this date.
    assert block(db, contact_context) == "quiet_hours"


@pytest.mark.parametrize("status", ["sent", "unknown"])
def test_late_delivery_counts_at_actual_finish_time(db, contact_context, status):
    user, _, _, now, _ = contact_context
    old = attempt(db, contact_context, hours=30)
    outbound = OutboundMessage(
        user_id=user.id,
        task_id=old.id,
        qq_id="710006001",
        content="合成联系",
        status=status,
        finished_at=now - timedelta(minutes=30),
    )
    db.add(outbound)
    db.flush()
    assert block(db, contact_context) == "min_interval"
    outbound.finished_at = now - timedelta(hours=5)
    attempt(db, contact_context, hours=6)
    db.flush()
    assert block(db, contact_context) == "daily_limit"


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_every_attempt_consumes_feedback_opportunity(db, contact_context, status):
    attempt(db, contact_context, status=status, same_activity=True)
    assert block(db, contact_context) == "feedback_used"


def test_recent_user_reply_waits_for_minimum_interval(db, contact_context):
    user, _, _, now, _ = contact_context
    user.last_active_at = now - timedelta(minutes=239)
    db.flush()
    assert block(db, contact_context) == "min_interval"
    user.last_active_at = now - timedelta(minutes=240)
    db.flush()
    assert block(db, contact_context) is None


@pytest.mark.parametrize("change", ["memory", "preferences", "persona", "activity"])
def test_contact_snapshot_invalidated_by_relevant_changes(db, contact_context, change):
    user, _, _, _, _ = contact_context
    current = attempt(db, contact_context, hours=0, same_activity=True)
    if change == "memory":
        user.memory_revision += 1
    elif change == "preferences":
        db.get(ContactPreference, user.id).revision += 1
    elif change == "persona":
        db.scalar(select(Persona).where(Persona.user_id == user.id)).version += 1
    else:
        user.last_active_at += timedelta(minutes=1)
    db.flush()
    assert block(db, contact_context, task=current) == "snapshot_changed"


def test_non_contact_and_other_accounts_do_not_consume_limits(db, contact_context):
    user, _, _, now, _ = contact_context
    other = Account(username="contact_other", password_hash="synthetic")
    db.add(other)
    db.flush()
    db.add_all(
        [
            Task(user_id=user.id, kind="scene", source="qq", status="completed", created_at=now),
            Task(
                user_id=other.id,
                kind="contact",
                source="autonomous",
                created_at=now,
                request=contact_snapshot(db, user.id),
            ),
        ]
    )
    db.flush()
    assert block(db, contact_context) is None


def test_contact_snapshot_does_not_depend_on_legacy_message_bodies(db, contact_context):
    user, chat, message, _, _ = contact_context
    before = contact_snapshot(db, user.id)
    db.add(
        ChatMessage(
            user_id=user.id,
            task_id=chat.id,
            role="user",
            content="私密原文不进入快照",
            sent_at=message.sent_at,
            created_at=message.created_at,
        )
    )
    db.flush()
    assert contact_snapshot(db, user.id) == before
    assert set(before) == {
        "activity_at",
        "memory_revision",
        "preferences_revision",
        "persona_version",
    }
    assert before["activity_at"] == user.last_active_at.isoformat()


@pytest.mark.parametrize("change", ["deleted", "excluded"])
def test_removing_legacy_feedback_does_not_restore_consumed_activity(db, contact_context, change):
    user, _, message, now, _ = contact_context
    attempt(db, contact_context, same_activity=True)
    before = contact_snapshot(db, user.id)
    if change == "deleted":
        db.delete(message)
    else:
        message.context_excluded = True
    db.flush()
    assert contact_snapshot(db, user.id) == before
    assert block(db, contact_context) == "feedback_used"
    # Fresh account activity, independent of any raw message, opens a later opportunity.
    user.last_active_at = now - timedelta(hours=4)
    db.flush()
    assert block(db, contact_context) is None


@pytest.mark.parametrize("status,error", [("failed", None), ("completed", "model_unknown")])
def test_terminal_chat_without_pending_delivery_does_not_block_contact(
    db, contact_context, status, error
):
    _, chat, message, _, _ = contact_context
    chat.status, chat.error = status, error
    db.delete(message)
    db.flush()
    assert block(db, contact_context) is None
