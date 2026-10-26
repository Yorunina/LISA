from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_contact_policy import contact_context as contact_context
from test_works import HEADERS
from test_works import owner as owner

from lisa.contact_policy import contact_block_reason
from lisa.interaction import interaction_context, interaction_view
from lisa.models import Account, ChatMessage, ContactPreference, Memory, Task


@pytest.fixture
def observation_user(db):
    user = Account(username="observed", password_hash="synthetic")
    db.add(user)
    db.flush()
    return user


NOW = datetime(2026, 10, 23, 4, tzinfo=UTC)


def messages(db, user_id, days, *, hour=12, content="合成消息", kind="text", **changes):
    chat = Task(user_id=user_id, kind="chat", source="qq", status="completed")
    db.add(chat)
    db.flush()
    result = []
    for day in days:
        sent_at = (NOW - timedelta(days=day)).replace(hour=hour)
        message = ChatMessage(
            user_id=user_id,
            task_id=chat.id,
            role="user",
            content=content,
            sent_at=sent_at,
            created_at=sent_at,
            kind=kind,
        )
        for key, value in changes.items():
            setattr(message, key, value)
        db.add(message)
        result.append(message)
    db.flush()
    return result, chat


def view(db, user):
    return interaction_view(db, user.id, now=NOW)


def test_repeated_dates_and_bursts_never_create_history_inferences(db, observation_user):
    user = observation_user
    messages(db, user.id, [1, 2, 3], hour=12)
    messages(db, user.id, [4] * 100, hour=0)
    result = view(db, user)
    assert result["sample_count"] == result["active_days"] == 0
    assert result["preferred_hours"] is None
    assert result["source_message_ids"] == []
    assert interaction_context(db, user.id, now=NOW) == ""
    assert db.scalar(select(func.count()).select_from(Memory)) == 0


@pytest.mark.parametrize("days", [[1] * 20, [1, 2], [1, 2, 3]])
def test_insufficient_or_tied_evidence_has_no_preference(db, observation_user, days):
    messages(db, observation_user.id, days, hour=12)
    if len(set(days)) == 3:
        messages(db, observation_user.id, days, hour=0)
    assert view(db, observation_user)["preferred_hours"] is None


def test_majority_required_across_active_dates(db, observation_user):
    messages(db, observation_user.id, [1, 2, 3], hour=12)
    messages(db, observation_user.id, [4, 5], hour=0)
    messages(db, observation_user.id, [6, 7], hour=6)
    messages(db, observation_user.id, [8, 9], hour=18)
    assert view(db, observation_user)["preferred_hours"] is None


def test_timezone_is_explicit_setting_without_history_inference(db, observation_user):
    messages(db, observation_user.id, [1, 2, 3], hour=12)
    assert view(db, observation_user)["timezone"] == "Asia/Shanghai"
    preference = ContactPreference(user_id=observation_user.id, timezone="America/New_York")
    db.add(preference)
    db.flush()
    result = view(db, observation_user)
    assert result["preferred_hours"] is None
    assert result["sample_count"] == 0 and result["source_message_ids"] == []
    assert result["timezone"] == "America/New_York"
    assert db.scalar(select(func.count()).select_from(Memory)) == 0


@pytest.mark.parametrize(
    "excluded",
    [
        "valid",
        "deleted",
        "excluded",
        "assistant",
        "web",
        "failed",
        "error",
        "other",
        "old",
        "future",
    ],
)
def test_legacy_feedback_is_never_observed(db, observation_user, excluded):
    items, task = messages(db, observation_user.id, [1, 2, 3])
    if excluded == "deleted":
        for item in items:
            item.deleted_at = NOW
    elif excluded == "excluded":
        for item in items:
            item.context_excluded = True
    elif excluded == "assistant":
        for item in items:
            item.role = "assistant"
    elif excluded == "web":
        task.source = "web"
    elif excluded == "failed":
        task.status = "failed"
    elif excluded == "error":
        task.error = "unknown"
    elif excluded == "other":
        other = Account(username="observed_other", password_hash="synthetic")
        db.add(other)
        db.flush()
        task.user_id = other.id
        for item in items:
            item.user_id = other.id
    elif excluded == "old":
        for item in items:
            item.sent_at = NOW - timedelta(days=15)
    elif excluded == "future":
        for item in items:
            item.sent_at = NOW + timedelta(hours=1)
    db.flush()
    assert view(db, observation_user)["sample_count"] == 0
    assert interaction_context(db, observation_user.id, now=NOW) == ""


def test_erasing_legacy_sources_leaves_no_inference_or_source_reference(db, observation_user):
    items, _ = messages(db, observation_user.id, [1, 2, 3])
    before = view(db, observation_user)
    for item in items:
        db.delete(item)
    db.flush()
    result = view(db, observation_user)
    assert result == before
    assert result["sample_count"] == 0 and result["source_message_ids"] == []
    assert result["preferred_hours"] is None
    assert interaction_context(db, observation_user.id, now=NOW) == ""


def test_large_legacy_history_is_not_sampled(db, observation_user):
    messages(db, observation_user.id, [1] * 200)
    messages(db, observation_user.id, [2, 3, 4])
    result = view(db, observation_user)
    assert result["sample_count"] == result["active_days"] == 0
    assert result["source_message_ids"] == []
    assert result["preferred_hours"] is None


@pytest.mark.parametrize("recent_length", [80, 10, 42])
def test_text_and_image_history_never_generate_length_or_trend_profiles(
    db, observation_user, recent_length
):
    messages(db, observation_user.id, [8, 9, 10, 11, 12], content="旧" * 40)
    messages(db, observation_user.id, [1, 2, 3, 4, 5], content="新" * recent_length)
    messages(db, observation_user.id, [1, 2, 3], content="媒体描述" * 500, kind="image")
    length = view(db, observation_user)["text_length"]
    assert length == {
        "sample_count": 0,
        "median": None,
        "recent_count": 0,
        "previous_count": 0,
        "recent_median": None,
        "previous_median": None,
        "trend": None,
    }
    assert interaction_context(db, observation_user.id, now=NOW) == ""
    assert db.scalar(select(func.count()).select_from(Memory)) == 0


def test_short_term_change_not_inferred_from_few_messages(db, observation_user):
    messages(db, observation_user.id, [8, 9, 10, 11], content="旧" * 40)
    messages(db, observation_user.id, [1, 2, 3, 4, 5], content="新" * 100)
    assert view(db, observation_user)["text_length"]["trend"] is None


def test_api_observations_read_only_and_user_scoped(client, db, owner):
    before = db.get(Account, owner).last_active_at
    result = client.get("/api/contact-settings").json()
    assert result["observations"]["sample_count"] == 0
    assert db.get(ContactPreference, owner) is None
    saved = client.post(
        "/api/contact-settings",
        headers=HEADERS,
        json={"observations": {"preferred_hours": {"start": 0, "end": 24}, "sample_count": 999}},
    )
    assert saved.status_code == 200
    assert saved.json()["observations"]["sample_count"] == 0
    db.expire_all()
    assert db.get(Account, owner).last_active_at == before


def test_legacy_hours_do_not_delay_eligible_contact(db, contact_context):
    user, _, _, now, settings = contact_context
    messages(db, user.id, [1, 2, 3], hour=12)
    user.last_active_at = now - timedelta(hours=4)
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) is None
    assert contact_block_reason(db, settings, user.id, now=now + timedelta(hours=2)) is None


def test_legacy_hours_cannot_override_explicit_minimum_interval(db, contact_context):
    user, _, _, now, settings = contact_context
    messages(db, user.id, [1, 2, 3], hour=12)
    now = now.replace(hour=10)
    user.last_active_at = now - timedelta(minutes=239)
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) == "min_interval"
    assert contact_block_reason(db, settings, user.id, now=now + timedelta(minutes=1)) is None


@pytest.mark.parametrize(
    "override,reason",
    [("inactive", "inactive"), ("quiet", "quiet_hours"), ("disabled", "user_disabled")],
)
def test_explicit_contact_limits_apply_with_legacy_history(db, contact_context, override, reason):
    user, _, _, now, settings = contact_context
    messages(db, user.id, [1, 2, 3], hour=12)
    preference = db.get(ContactPreference, user.id)
    if override == "inactive":
        user.last_active_at = now - timedelta(hours=24)
    elif override == "quiet":
        preference.quiet_start, preference.quiet_end = 0, 1439
    else:
        preference.enabled = False
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) == reason
