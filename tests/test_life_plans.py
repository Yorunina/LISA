from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from lisa.life_plans import (
    _default_items,
    _merge_generated_items,
    apply_explicit_plan_text,
    enqueue_agent_plan,
    ensure_current_plan,
    execute_life_plan,
    week_start_for,
)
from lisa.models import Account, ChatMessage, LifeWeekPlan, Task

HEADERS = {"X-LISA-Request": "1"}


@pytest.fixture
def owner(client, invites):
    response = client.post(
        "/api/auth/register",
        headers=HEADERS,
        json={
            "username": "life_plan_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def test_week_plan_generate_edit_and_account_isolation(client, owner):
    generated = client.post("/api/life-plan/generate", headers=HEADERS, json={})
    assert generated.status_code == 200, generated.text
    plan = generated.json()
    assert len(plan["items"]) == 49
    assert all(item["status"] == "tentative" for item in plan["items"])
    assert all(item["time"] for item in plan["items"])
    assert sum(item["title"] == "睡眠" for item in plan["items"]) == 7
    assert all(sum(item["date"] == day for item in plan["items"]) == 7 for day in {
        item["date"] for item in plan["items"]
    })

    item = {
        key: value
        for key, value in (plan["items"][0] | {"title": "周一读诗", "status": "confirmed"}).items()
        if key not in {"source_deleted", "source_label"}
    }
    saved = client.put(
        f"/api/life-plan/{plan['id']}",
        headers=HEADERS,
        json={"revision": plan["revision"], "state": "active", "items": [item]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["items"][0]["source"] == "user"
    assert client.get("/api/life-plan").json()["current"]["items"][0]["title"] == "周一读诗"

    cancelled = client.put(
        f"/api/life-plan/{plan['id']}",
        headers=HEADERS,
        json={"revision": saved.json()["revision"], "state": "cancelled", "items": [item]},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    stale = client.put(
        f"/api/life-plan/{plan['id']}",
        headers=HEADERS,
        json={"revision": plan["revision"], "state": "active", "items": [item]},
    )
    assert stale.status_code == 409


def test_generation_uses_agent_items_when_model_returns_them(client, owner, monkeypatch):
    async def fake_generate(settings, engine, task_id, user_id, week_start, current_items):
        return [
            {
                "id": f"agent-{offset}-{index}",
                "date": (week_start + timedelta(days=offset)).isoformat(),
                "time": f"{index * 6:02d}:00–{index * 6 + 2:02d}:00",
                "title": "人格生成的生活安排",
                "detail": "根据 LISA 的人格与记忆生成。",
                "status": "tentative",
                "source": "agent",
                "source_message_id": None,
            }
            for offset in range(7)
            for index in range(4)
        ]

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", fake_generate)
    response = client.post("/api/life-plan/generate", headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 28
    assert response.json()["items"][0]["title"] == "人格生成的生活安排"


def test_regenerate_keeps_confirmed_slot_and_daily_rhythm(client, owner):
    generated = client.post("/api/life-plan/generate", headers=HEADERS, json={}).json()
    confirmed = next(item for item in generated["items"] if item["time"] == "14:00–18:00")
    confirmed = {**confirmed, "title": "周三去看展", "status": "confirmed"}
    confirmed = {
        key: value
        for key, value in confirmed.items()
        if key not in {"source_deleted", "source_label"}
    }
    saved = client.put(
        f"/api/life-plan/{generated['id']}",
        headers=HEADERS,
        json={"revision": generated["revision"], "state": "active", "items": [confirmed]},
    )
    assert saved.status_code == 200

    regenerated = client.post(
        "/api/life-plan/generate",
        headers=HEADERS,
        json={"force": True},
    )
    assert regenerated.status_code == 200
    items = regenerated.json()["items"]
    assert len(items) == 49
    assert next(item for item in items if item["time"] == "14:00–18:00")["title"] == "周三去看展"


def test_chat_plan_patch_requires_explicit_day_and_change(db, monkeypatch):
    monkeypatch.setattr("lisa.life_plans.week_start_for", lambda *args: date(2026, 10, 19))
    user = Account(username="plan_patch_user", password_hash="unused")
    db.add(user)
    db.flush()
    source = ChatMessage(
        user_id=user.id,
        role="user",
        content="把周六改成看电影。",
        sent_at=datetime.now().astimezone(),
    )
    db.add(source)
    db.flush()
    db.add(
        LifeWeekPlan(
            user_id=user.id,
            week_start=date(2026, 10, 19),
            week_end=date(2026, 10, 25),
            items=[
                {
                    "id": "item-1",
                    "date": "2026-10-24",
                    "time": "晚上",
                    "title": "读书",
                    "detail": "",
                    "status": "tentative",
                    "source": "agent",
                    "source_message_id": None,
                }
            ],
        )
    )
    db.commit()

    changed = apply_explicit_plan_text(db, user.id, [source.id], [source.content])
    assert changed
    db.commit()
    plan = db.query(LifeWeekPlan).one()
    assert plan.items[0]["title"] == "看电影"
    assert plan.items[0]["source"] == "chat"

    added_source = ChatMessage(
        user_id=user.id,
        role="user",
        content="周四下午安排去看展。",
        sent_at=datetime.now().astimezone(),
    )
    db.add(added_source)
    db.flush()
    assert apply_explicit_plan_text(
        db,
        user.id,
        [added_source.id],
        [added_source.content],
    )
    db.commit()
    plan = db.query(LifeWeekPlan).one()
    assert any(item["title"] == "去看展" and item["source"] == "chat" for item in plan.items)

    ignored = apply_explicit_plan_text(db, user.id, [source.id], ["最近想休息。"])
    assert not ignored


def test_chat_plan_patch_targets_the_matching_daily_block(db, monkeypatch):
    monkeypatch.setattr("lisa.life_plans.week_start_for", lambda *args: date(2026, 10, 19))
    user = Account(username="plan_patch_blocks", password_hash="unused")
    db.add(user)
    db.flush()
    source = ChatMessage(
        user_id=user.id,
        role="user",
        content="周四下午安排去看展。",
        sent_at=datetime.now().astimezone(),
    )
    db.add(source)
    db.flush()
    db.add(
        LifeWeekPlan(
            user_id=user.id,
            week_start=date(2026, 10, 19),
            week_end=date(2026, 10, 25),
            items=_default_items(date(2026, 10, 19)),
        )
    )
    db.commit()

    assert apply_explicit_plan_text(db, user.id, [source.id], [source.content])
    db.commit()
    plan = db.query(LifeWeekPlan).one()
    thursday = [item for item in plan.items if item["date"] == "2026-10-22"]
    assert next(item for item in thursday if item["time"] == "23:00–07:00")["title"] == "睡眠"
    assert next(item for item in thursday if item["time"] == "14:00–18:00")["title"] == "去看展"


def test_regeneration_reserves_capacity_for_every_user_item():
    start = date(2026, 10, 19)
    protected = [
        {**item, "source": "user", "time": f"用户时段-{index // 2}"}
        for index, item in enumerate(_default_items(start))
    ]
    plan = LifeWeekPlan(items=protected)
    _merge_generated_items(plan, _default_items(start))
    assert {item["id"] for item in protected} <= {item["id"] for item in plan.items}
    assert len(plan.items) <= 56


@pytest.fixture
def queued_plan(db, owner):
    user = db.get(Account, owner)
    user.last_active_at = datetime.now(UTC)
    plan = ensure_current_plan(db, owner)
    plan_id = plan.id
    assert enqueue_agent_plan(db, owner)
    task = db.query(Task).filter_by(user_id=owner, kind="life_plan").one()
    task_id = task.id
    db.commit()
    return task_id, plan_id


@pytest.mark.asyncio
async def test_queued_generation_finishes_across_transaction_boundary(
    settings, engine, db, owner, queued_plan, monkeypatch,
):
    task_id, plan_id = queued_plan

    async def generate(settings, engine, task_id, user_id, start, current_items):
        assert user_id == owner
        return [{**item, "title": "已生成的日程"} for item in _default_items(start)]

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", generate)
    await execute_life_plan(settings, engine, str(task_id))
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(LifeWeekPlan, plan_id).items[0]["title"] == "已生成的日程"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["inactive", "old_week", "cancelled_plan", "replaced_plan"])
async def test_queued_generation_does_not_start_after_pause_or_for_an_old_week(
    settings, engine, db, owner, queued_plan, monkeypatch, change,
):
    task_id, plan_id = queued_plan
    if change == "inactive":
        db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(hours=25)
    elif change == "old_week":
        plan = db.get(LifeWeekPlan, plan_id)
        plan.week_start -= timedelta(days=7)
        plan.week_end -= timedelta(days=7)
        task = db.get(Task, task_id)
        task.request = {**task.request, "week_start": plan.week_start.isoformat()}
    elif change == "cancelled_plan":
        db.get(LifeWeekPlan, plan_id).state = "cancelled"
    else:
        db.delete(db.get(LifeWeekPlan, plan_id))
        db.flush()
        plan_id = ensure_current_plan(db, owner).id
    db.commit()

    async def unexpected(*args):
        pytest.fail("paused or expired plans must not call the model")

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", unexpected)
    await execute_life_plan(settings, engine, str(task_id))
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(LifeWeekPlan, plan_id).revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["disabled", "cancelled", "memory", "deleted", "edited", "account_deleted"]
)
async def test_queued_generation_rechecks_adoption_boundary(
    settings, engine, db, owner, queued_plan, monkeypatch, change,
):
    task_id, plan_id = queued_plan

    async def generate(settings, engine, task_id, user_id, start, current_items):
        if change == "disabled":
            db.get(Account, owner).active = False
        elif change == "cancelled":
            db.get(Task, task_id).status = "cancelled"
        elif change == "memory":
            db.get(Account, owner).memory_revision += 1
        elif change == "deleted":
            db.delete(db.get(LifeWeekPlan, plan_id))
        elif change == "account_deleted":
            db.delete(db.get(Account, owner))
        else:
            plan = db.get(LifeWeekPlan, plan_id)
            plan.items = [{**plan.items[0], "title": "用户已修改", "source": "user"}]
            plan.revision += 1
        db.commit()
        return [{**item, "title": "迟到的生成结果"} for item in _default_items(start)]

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", generate)
    await execute_life_plan(settings, engine, str(task_id))
    db.expire_all()
    task = db.get(Task, task_id)
    assert task is None if change == "account_deleted" else task.status == "cancelled"
    plan = db.get(LifeWeekPlan, plan_id)
    assert plan is None or all(item["title"] != "迟到的生成结果" for item in plan.items)


@pytest.mark.parametrize(
    "change", ["deleted", "edited", "disabled", "cancelled", "memory", "account_deleted"]
)
def test_web_generation_does_not_restore_deleted_or_stale_plan(
    client, db, owner, monkeypatch, change,
):
    original = client.post("/api/life-plan/generate", headers=HEADERS, json={}).json()
    plan_id = UUID(original["id"])

    async def generate(settings, engine, task_id, user_id, start, current_items):
        if change == "deleted":
            db.delete(db.get(LifeWeekPlan, plan_id))
        elif change == "edited":
            plan = db.get(LifeWeekPlan, plan_id)
            plan.items = [{**plan.items[0], "title": "保留网页修改", "source": "user"}]
            plan.revision += 1
        elif change == "disabled":
            db.get(Account, owner).active = False
        elif change == "cancelled":
            db.get(Task, task_id).status = "cancelled"
        elif change == "account_deleted":
            db.delete(db.get(Account, owner))
        else:
            db.get(Account, owner).memory_revision += 1
        db.commit()
        return [{**item, "title": "过期结果"} for item in _default_items(start)]

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", generate)
    response = client.post("/api/life-plan/generate", headers=HEADERS, json={"force": True})
    assert response.status_code == (401 if change in {"disabled", "account_deleted"} else 409)
    db.expire_all()
    plan = db.get(LifeWeekPlan, plan_id)
    assert plan is None or all(item["title"] != "过期结果" for item in plan.items)
    task = db.query(Task).filter_by(user_id=owner, kind="life_plan").order_by(
        Task.created_at.desc()
    ).first()
    assert task is None if change == "account_deleted" else task.status == "cancelled"


@pytest.mark.parametrize("history", [False, True])
def test_future_plans_do_not_hide_the_current_week(client, db, owner, history):
    start = week_start_for(db, owner)
    for offset in range(10):
        day = start + timedelta(weeks=offset)
        db.add(LifeWeekPlan(
            user_id=owner, week_start=day, week_end=day + timedelta(days=6), items=[],
        ))
    db.commit()
    response = client.get("/api/life-plan", params={"history": str(history).lower()})
    assert response.status_code == 200
    assert response.json()["current"]["week_start"] == start.isoformat()
    assert len(response.json()["plans"]) == (8 if history else 0)


def test_concurrent_first_generation_preserves_the_first_saved_plan(client, db, owner, monkeypatch):
    async def generate(settings, engine, task_id, user_id, start, current_items):
        db.add(LifeWeekPlan(
            user_id=owner, week_start=start, week_end=start + timedelta(days=6),
            items=[{**_default_items(start)[0], "title": "先保存的周表"}],
        ))
        db.commit()
        return _default_items(start)

    monkeypatch.setattr("lisa.life_plans._generate_agent_items", generate)
    response = client.post("/api/life-plan/generate", headers=HEADERS, json={})
    assert response.status_code == 409
    plan = db.query(LifeWeekPlan).filter_by(user_id=owner).one()
    assert plan.items[0]["title"] == "先保存的周表" and plan.revision == 1
