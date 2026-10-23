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


def test_repeat_dates_outweigh_single_day_burst(db, observation_user):
    user = observation_user
    messages(db, user.id, [1, 2, 3], hour=12)
    messages(db, user.id, [4] * 100, hour=0)
    result = view(db, user)
    assert result["sample_count"] == 103
    assert result["active_days"] == 4
    assert result["preferred_hours"] == {"start": 18, "end": 24, "active_days": 3}
    assert result["kind"] == "inference"
    assert len(result["source_message_ids"]) == 103
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


def test_timezone_changes_local_slots_without_writing_evidence(db, observation_user):
    messages(db, observation_user.id, [1, 2, 3], hour=12)
    assert view(db, observation_user)["preferred_hours"]["start"] == 18
    preference = ContactPreference(user_id=observation_user.id, timezone="America/New_York")
    db.add(preference)
    db.flush()
    result = view(db, observation_user)
    assert result["preferred_hours"] == {"start": 6, "end": 12, "active_days": 3}
    assert result["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "excluded",
    ["deleted", "excluded", "assistant", "web", "failed", "error", "other", "old", "future"],
)
def test_only_valid_owned_qq_feedback_is_observed(db, observation_user, excluded):
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
    else:
        for item in items:
            item.sent_at = NOW + timedelta(hours=1)
    db.flush()
    assert view(db, observation_user)["sample_count"] == 0
    assert interaction_context(db, observation_user.id, now=NOW) == ""


def test_removing_source_immediately_removes_time_inference(db, observation_user):
    items, _ = messages(db, observation_user.id, [1, 2, 3])
    assert view(db, observation_user)["preferred_hours"]
    items[0].deleted_at = NOW
    items[0].content = ""
    db.flush()
    result = view(db, observation_user)
    assert result["preferred_hours"] is None
    assert str(items[0].id) not in result["source_message_ids"]


def test_window_is_bounded_to_most_recent_200(db, observation_user):
    messages(db, observation_user.id, [1] * 200)
    older, _ = messages(db, observation_user.id, [2, 3, 4])
    result = view(db, observation_user)
    assert result["sample_count"] == 200
    assert result["active_days"] == 1
    assert str(older[0].id) not in result["source_message_ids"]


@pytest.mark.parametrize("recent_length,trend", [(80, "longer"), (10, "shorter"), (42, None)])
def test_text_lengths_and_recent_change_require_both_weeks(
    db, observation_user, recent_length, trend
):
    messages(db, observation_user.id, [8, 9, 10, 11, 12], content="旧" * 40)
    messages(db, observation_user.id, [1, 2, 3, 4, 5], content="新" * recent_length)
    messages(db, observation_user.id, [1, 2, 3], content="媒体描述" * 500, kind="image")
    length = view(db, observation_user)["text_length"]
    assert length["sample_count"] == 10
    assert length["median"] == (40 + recent_length) / 2
    assert length["recent_count"] == length["previous_count"] == 5
    assert length["trend"] == trend
    context = interaction_context(db, observation_user.id, now=NOW)
    assert len(context) <= 1000 and "inference" in context
    assert "媒体描述" not in context and "旧" * 40 not in context


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


def test_preferred_hours_delay_at_most_two_hours(db, contact_context):
    user, _, _, now, settings = contact_context
    messages(db, user.id, [1, 2, 3], hour=12)  # Local 18-24.
    user.last_active_at = now - timedelta(hours=4)
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) == "preferred_hours"
    assert contact_block_reason(db, settings, user.id, now=now + timedelta(hours=2)) is None


def test_arrival_of_preferred_hours_ends_delay_early(db, contact_context):
    user, _, _, now, settings = contact_context
    messages(db, user.id, [1, 2, 3], hour=12)
    now = now.replace(hour=9)  # Local 17:00, preferred starts at 18:00.
    user.last_active_at = now - timedelta(hours=4)
    db.flush()
    assert contact_block_reason(db, settings, user.id, now=now) == "preferred_hours"
    assert contact_block_reason(db, settings, user.id, now=now + timedelta(hours=1)) is None


@pytest.mark.parametrize(
    "override,reason",
    [("inactive", "inactive"), ("quiet", "quiet_hours"), ("disabled", "user_disabled")],
)
def test_hard_contact_limits_precede_observed_hours(db, contact_context, override, reason):
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
