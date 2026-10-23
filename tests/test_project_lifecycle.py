from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from procrastinate.exceptions import JobRetry
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_author import pending_task
from test_works import HEADERS, RESULT, document
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.author import execute_author
from lisa.models import (
    Account,
    ActivityPlan,
    Chapter,
    ChapterVersion,
    OutboundMessage,
    Persona,
    Project,
    Task,
)
from lisa.scheduling import enqueue_due_plans
from lisa.works import ProjectInput, create_project


def new_project(client, title="另一本日记"):
    response = client.post("/api/projects", json={"title": title, "kind": "diary"}, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def test_new_project_preserves_old_work_persona_and_activity(client, db, owner, project, chapter):
    old = db.get(Project, UUID(project["id"]))
    old.worldbook, old.characters, old.agreement = "旧世界", "旧人物", "旧共识"
    user = db.get(Account, owner)
    user.last_active_at = datetime.now(UTC) - timedelta(days=2)
    activity = user.last_active_at
    persona_id = db.scalar(select(Persona.id))
    db.commit()
    created = new_project(client)
    assert created["is_current"] and not created["completed"]
    assert all(created[key] == "" for key in ("worldbook", "characters", "agreement", "summary"))
    assert client.get(f"/api/projects/{created['id']}/chapters").json() == []
    db.expire_all()
    assert not old.is_current and not old.completed and old.worldbook == "旧世界"
    assert db.get(Chapter, chapter).project_id == old.id
    assert db.scalar(select(ChapterVersion)).version == 1
    assert db.scalar(select(Persona.id)) == persona_id
    assert db.get(Account, owner).last_active_at == activity
    assert not list(db.scalars(select(Task)))
    assert not list(db.scalars(select(OutboundMessage)))


def test_read_edit_complete_and_continue_are_distinct(client, db, owner, project, chapter):
    created = new_project(client)
    old_id = project["id"]
    old = client.get(f"/api/projects/{old_id}").json()
    assert not old["is_current"] and not old["completed"]
    assert client.post(f"/api/projects/{old_id}/complete", headers=HEADERS).json()["completed"]
    response = client.put(
        f"/api/chapters/{chapter}",
        json={"title": "旧作修订", "content": document("修订后的结尾。")},
        headers=HEADERS,
    )
    assert response.status_code == 200 and response.json()["version"] == 2
    assert (
        client.post(
            f"/api/projects/{old_id}/tasks",
            json={"kind": "scene", "goal": "新增场景"},
            headers=HEADERS,
        ).status_code
        == 409
    )
    revised = client.get(f"/api/projects/{old_id}").json()
    assert revised["completed"] and not revised["is_current"]
    resumed = client.post(f"/api/projects/{old_id}/activate", headers=HEADERS).json()
    assert resumed["is_current"] and not resumed["completed"]
    other = client.get(f"/api/projects/{created['id']}").json()
    assert not other["is_current"] and not other["completed"]
    assert not list(db.scalars(select(Task)))


@pytest.mark.asyncio
async def test_completed_noncurrent_work_accepts_only_explicit_local_rewrite(
    client, db, settings, engine, owner, project, chapter
):
    client.post(f"/api/projects/{project['id']}/complete", headers=HEADERS)
    created = new_project(client)
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "kind": "rewrite",
            "goal": "仅调整结尾",
            "chapter_id": str(chapter),
            "target_text": "旧的结尾。",
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    await execute_author(
        settings, engine, response.json()["id"], generate=AsyncMock(return_value=RESULT)
    )
    db.expire_all()
    assert db.get(Task, UUID(response.json()["id"])).adopted_at
    old = db.get(Project, UUID(project["id"]))
    assert old.completed and not old.is_current
    assert db.get(Project, UUID(created["id"])).is_current
    assert db.get(Chapter, chapter).version == 2


@pytest.mark.parametrize("action", ["create", "activate", "complete"])
def test_state_changes_invalidate_only_unstarted_autonomous_work(
    client, db, owner, project, action
):
    other_id = new_project(client)["id"] if action == "activate" else None
    if other_id:
        client.post(f"/api/projects/{project['id']}/activate", headers=HEADERS)
    auto_id = pending_task(db, owner, project, source="autonomous")
    manual_id = pending_task(db, owner, project)
    plan = ActivityPlan(
        user_id=owner,
        project_id=UUID(project["id"]),
        goal="原计划",
        activity_at=db.get(Account, owner).last_active_at,
        memory_revision=0,
        due_at=datetime.now(UTC),
    )
    db.add(plan)
    db.commit()
    if action == "create":
        new_project(client)
    else:
        target = other_id if action == "activate" else project["id"]
        assert client.post(f"/api/projects/{target}/{action}", headers=HEADERS).status_code == 200
    db.expire_all()
    assert db.get(Task, auto_id).status == "cancelled"
    assert db.get(Task, manual_id).status == "pending"
    assert plan.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["saved", "cancelled", "budget"])
async def test_switch_during_generation_keeps_old_scope_and_allows_cancel(
    client, db, settings, engine, owner, project, outcome
):
    from lisa.model_calls import BudgetExceeded

    old_id = pending_task(db, owner, project, source="autonomous")
    created = None

    async def generate(*args):
        nonlocal created
        created = new_project(client)
        handoff = client.get("/api/projects/handoff").json()["finishing_tasks"]
        assert [item["id"] for item in handoff] == [str(old_id)]
        assert handoff[0]["project_title"] == project["title"] and handoff[0]["can_cancel"]
        if outcome == "cancelled":
            client.post(f"/api/tasks/{old_id}/cancel", headers=HEADERS)
        if outcome == "budget":
            raise BudgetExceeded()
        return RESULT

    await execute_author(settings, engine, old_id, generate=generate)
    db.expire_all()
    old = db.get(Task, old_id)
    assert (
        old.status == {"saved": "completed", "cancelled": "cancelled", "budget": "failed"}[outcome]
    )
    assert client.get("/api/projects/handoff").json() == {"finishing_tasks": []}
    assert client.get(f"/api/projects/{created['id']}/chapters").json() == []
    assert len(client.get(f"/api/projects/{project['id']}/chapters").json()) == (
        1 if outcome == "saved" else 0
    )


@pytest.mark.asyncio
async def test_new_autonomous_task_waits_for_old_work_but_not_notification(
    client, db, settings, engine, owner, project
):
    old_id = pending_task(db, owner, project, source="qq")
    old = db.get(Task, old_id)
    old.status, old.started_at = "running", datetime.now(UTC)
    db.commit()
    created = new_project(client)
    new_id = pending_task(db, owner, created, source="autonomous")
    generate = AsyncMock(return_value=RESULT)
    with pytest.raises(JobRetry):
        await execute_author(settings, engine, new_id, generate=generate)
    generate.assert_not_awaited()
    db.expire_all()
    assert db.get(Task, new_id).started_at is None
    old.status, old.finished_at = "completed", datetime.now(UTC)
    db.add(
        OutboundMessage(
            user_id=owner, task_id=old_id, qq_id="720000001", content="旧结果通知", status="pending"
        )
    )
    db.commit()
    await execute_author(settings, engine, new_id, generate=generate)
    generate.assert_awaited_once()
    db.expire_all()
    assert db.get(Task, new_id).adopted_at
    assert db.scalar(select(OutboundMessage)).status == "pending"


def test_due_plan_waits_then_rechecks_activity_after_old_task_finishes(
    client, db, settings, owner, project
):
    settings = settings.model_copy(update={"autonomy_enabled": True})
    old_id = pending_task(db, owner, project)
    old = db.get(Task, old_id)
    old.status, old.started_at = "running", datetime.now(UTC)
    db.commit()
    created = new_project(client)
    current = db.get(Project, UUID(created["id"]))
    current.agreement = "新作自己的共识"
    user = db.get(Account, owner)
    plan = ActivityPlan(
        user_id=owner,
        project_id=current.id,
        goal="新作下一步",
        due_at=datetime.now(UTC),
        activity_at=user.last_active_at,
        memory_revision=user.memory_revision,
    )
    db.add(plan)
    db.commit()
    assert enqueue_due_plans(db, settings) == 0
    assert plan.status == "pending"
    old.status = "completed"
    assert enqueue_due_plans(db, settings, now=user.last_active_at + timedelta(hours=24)) == 0
    assert plan.status == "cancelled"


def test_project_actions_and_handoff_are_owner_scoped(client, db, owner, project):
    other = Account(username="other_project_owner", password_hash="synthetic")
    db.add(other)
    db.flush()
    hidden = Project(user_id=other.id, title="别人的旧作", is_current=False)
    db.add(hidden)
    db.flush()
    db.add(
        Task(
            user_id=other.id,
            project_id=hidden.id,
            kind="scene",
            status="running",
            started_at=datetime.now(UTC),
        )
    )
    db.commit()
    for action in ("activate", "complete"):
        assert (
            client.post(f"/api/projects/{hidden.id}/{action}", headers=HEADERS).status_code == 404
        )
    assert client.get(f"/api/projects/{hidden.id}").status_code == 404
    assert client.get("/api/projects/handoff").json() == {"finishing_tasks": []}
    assert [item["id"] for item in client.get("/api/projects").json()] == [project["id"]]


def test_concurrent_creation_preserves_one_current_work(engine, db, owner, project):
    barrier = Barrier(2)

    def create(title):
        with Session(engine) as session:
            user = session.get(Account, owner)
            barrier.wait()
            return create_project(ProjectInput(title=title, kind="novel"), user, session).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, title) for title in ("另一部", "又一部")]
        ids = [future.result() for future in futures]
    db.expire_all()
    current = list(db.scalars(select(Project).where(Project.user_id == owner, Project.is_current)))
    assert len(current) == 1 and current[0].id in ids
    assert len(list(db.scalars(select(Project).where(Project.user_id == owner)))) == 3
