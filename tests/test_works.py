from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from lisa.models import (
    Account,
    BudgetDay,
    Chapter,
    ChapterVersion,
    ModelCall,
    Persona,
    Project,
    QQBinding,
    Task,
)
from lisa.works import adopt_result, create_writing_task

HEADERS = {"X-LISA-Request": "1"}
RESULT = {
    "title": "雨中的车站",
    "paragraphs": ["她在雨中找到一封信。"],
    "summary": "她找到一封信。",
    "plot_state": "信尚未打开。",
}


def document(content):
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": content}]}],
    }


@pytest.fixture
def owner(client, db, invites):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "works_reader",
            "password": "synthetic-password-2026",
            "invite_code": invites[0],
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    user_id = UUID(response.json()["id"])
    db.add(QQBinding(user_id=user_id, qq_id="720000001"))
    db.add(Persona(user_id=user_id, name="小莎", prompt="合成作品测试。", version=1))
    db.commit()
    return user_id


@pytest.fixture
def project(client, owner):
    response = client.post(
        "/api/projects", json={"title": "雨中的信", "kind": "novel"}, headers=HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def chapter(db, project):
    item = Chapter(
        project_id=UUID(project["id"]),
        title="第一章",
        position=1,
        content=document("开头保留。旧的结尾。后文保留。"),
        version=1,
    )
    db.add(item)
    db.flush()
    db.add(
        ChapterVersion(
            chapter_id=item.id,
            version=1,
            title=item.title,
            content=item.content,
            source="web",
        )
    )
    item_id = item.id
    db.commit()
    return item_id


def running_task(db, owner, project, *, kind="scene", chapter_id=None, target_text=None):
    work = db.get(Project, UUID(project["id"]))
    work.agreement = "一个雨中发现信件的有限场景。"
    task = create_writing_task(
        db,
        db.get(Account, owner),
        work,
        kind,
        "写一个有限场景",
        chapter_id=chapter_id,
        target_text=target_text,
    )
    task.status = "running"
    task.started_at = datetime.now(UTC)
    task_id = task.id
    db.commit()
    return task_id


def test_new_work_is_not_consensus_and_cannot_start_a_scene(client, db, project):
    assert project["agreement"] == ""
    assert project["is_current"] is True and project["completed"] is False
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "kind": "scene",
            "goal": "写一个短场景",
        },
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert client.get(f"/api/projects/{project['id']}/chapters").json() == []


def test_chat_tasks_are_not_exposed_as_writing_tasks(client, db, project, owner):
    chat = Task(user_id=owner, project_id=UUID(project["id"]), kind="chat", status="failed")
    db.add(chat)
    db.flush()
    task_id = chat.id
    db.commit()
    assert client.get(f"/api/projects/{project['id']}/tasks").json() == []
    for action in ("cancel", "retry"):
        assert client.post(f"/api/tasks/{task_id}/{action}", headers=HEADERS).status_code == 404
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"


def test_explicit_chapter_save_noop_version_and_project_materials(client, db, project, chapter):
    current = client.get(f"/api/chapters/{chapter}").json()
    unchanged = client.put(
        f"/api/chapters/{chapter}",
        json={
            "title": current["title"],
            "content": current["content"],
        },
        headers=HEADERS,
    )
    assert unchanged.status_code == 200 and unchanged.json()["version"] == 1
    revision = client.get(f"/api/projects/{project['id']}").json()["index_revision"]
    changed = client.put(
        f"/api/chapters/{chapter}",
        json={
            "title": current["title"],
            "content": document("明确保存后的新正文。"),
        },
        headers=HEADERS,
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.json()["text"] == "明确保存后的新正文。"
    assert client.get(f"/api/projects/{project['id']}").json()["index_revision"] == revision
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 2
    materials = {
        "worldbook": "雨城没有电灯。",
        "constraints": "主角不得死亡。",
        "characters": "主角：林雨。",
        "summary": "主角抵达车站。",
        "plot_state": "信未打开。",
        "agreement": "写发现信的场景。",
        "preferences": "推测偏好：慢节奏。",
        "tentative": "暂定：信来自旧友。",
        "author_prompt": "保持第三人称。",
    }
    saved = client.put(f"/api/projects/{project['id']}", json=materials, headers=HEADERS)
    assert saved.status_code == 200
    assert saved.json()["index_revision"] > revision
    for key, value in materials.items():
        assert saved.json()[key] == value
    assert client.get(f"/api/chapters/{chapter}").json() == changed.json()


def test_other_user_cannot_read_edit_or_operate_work(client, db, project, chapter, owner):
    task_id = running_task(db, owner, project)
    other_id = uuid4()
    db.add(Account(id=other_id, username="other_works_reader", password_hash="unused"))
    other = Project(user_id=other_id, title="别人作品", agreement="别人的共识", is_current=True)
    db.add(other)
    db.flush()
    other_chapter = Chapter(
        project_id=other.id, title="秘密", position=1, content=document("私人正文")
    )
    db.add(other_chapter)
    db.flush()
    other_task = Task(
        user_id=other_id, project_id=other.id, kind="scene", source="web", status="pending"
    )
    db.add(other_task)
    db.flush()
    other_project_id, other_chapter_id, other_task_id = other.id, other_chapter.id, other_task.id
    db.commit()
    assert [p["id"] for p in client.get("/api/projects").json()] == [project["id"]]
    for path in [
        f"/api/projects/{other_project_id}",
        f"/api/projects/{other_project_id}/chapters",
        f"/api/projects/{other_project_id}/chapters?summary=true",
        f"/api/projects/{other_project_id}/tasks",
        f"/api/chapters/{other_chapter_id}",
    ]:
        assert client.get(path).status_code == 404
    assert (
        client.put(
            f"/api/projects/{other_project_id}", json={"summary": "overwrite"}, headers=HEADERS
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/chapters/{other_chapter_id}",
            json={"title": "overwrite", "content": document("overwrite")},
            headers=HEADERS,
        ).status_code
        == 404
    )
    for action in ("cancel", "retry"):
        assert (
            client.post(f"/api/tasks/{other_task_id}/{action}", headers=HEADERS).status_code == 404
        )
    assert (
        client.post(
            f"/api/projects/{project['id']}/tasks",
            json={
                "kind": "rewrite",
                "goal": "改写",
                "chapter_id": str(other_chapter_id),
                "target_text": "私人正文",
            },
            headers=HEADERS,
        ).status_code
        == 404
    )
    assert client.get(f"/api/projects/{project['id']}/tasks").json()[0]["id"] == str(task_id)


def test_adoption_is_idempotent_and_cancel_after_save_preserves_text(client, db, project, owner):
    task_id = running_task(db, owner, project)
    adopted = adopt_result(db, db.get(Task, task_id), RESULT)
    chapter_id = adopted.id
    db.commit()
    repeated = adopt_result(db, db.get(Task, task_id), {**RESULT, "paragraphs": ["不得重复覆盖"]})
    db.commit()
    assert repeated.id == chapter_id
    assert db.scalar(select(func.count()).select_from(Chapter)) == 1
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1
    saved = client.get(f"/api/chapters/{chapter_id}").json()
    assert saved["text"] == RESULT["paragraphs"][0]
    cancellation = client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    assert cancellation.status_code == 200
    assert client.get(f"/api/chapters/{chapter_id}").json() == saved
    db.expire_all()
    assert db.get(Task, task_id).adopted_at is not None
    assert db.get(Project, UUID(project["id"])).plot_state == RESULT["plot_state"]


def test_cancel_before_adoption_discards_late_result(client, db, project, chapter, owner):
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="旧的结尾。"
    )
    before = client.get(f"/api/chapters/{chapter}").json()
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
    db.expire_all()
    assert adopt_result(db, db.get(Task, task_id), RESULT) is None
    db.commit()
    assert client.get(f"/api/chapters/{chapter}").json() == before
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1
    assert db.get(Task, task_id).adopted_at is None


def test_rewrite_preserves_text_outside_selected_fragment(client, db, project, chapter, owner):
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="旧的结尾。"
    )
    adopt_result(db, db.get(Task, task_id), {**RESULT, "paragraphs": ["新的结尾。"]})
    db.commit()
    result = client.get(f"/api/chapters/{chapter}").json()
    assert result["text"] == "开头保留。新的结尾。后文保留。"
    assert result["version"] == 2


def test_adoption_rollback_keeps_text_versions_and_summary_together(client, db, project, owner):
    task_id = running_task(db, owner, project)
    before = client.get(f"/api/projects/{project['id']}").json()
    adopt_result(db, db.get(Task, task_id), RESULT)
    db.rollback()
    assert client.get(f"/api/projects/{project['id']}").json() == before
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 0
    assert db.get(Task, task_id).adopted_at is None


def test_rewrite_rejects_ambiguous_or_missing_selection(client, db, project, chapter):
    for target, expected_status in [("不存在", 409), ("", 400)]:
        response = client.post(
            f"/api/projects/{project['id']}/tasks",
            json={
                "kind": "rewrite",
                "goal": "只改指定片段",
                "chapter_id": str(chapter),
                "target_text": target,
            },
            headers=HEADERS,
        )
        assert response.status_code == expected_status
    client.put(
        f"/api/chapters/{chapter}",
        json={"title": "第一章", "content": document("重复。重复。")},
        headers=HEADERS,
    )
    assert (
        client.post(
            f"/api/projects/{project['id']}/tasks",
            json={
                "kind": "rewrite",
                "goal": "改写",
                "chapter_id": str(chapter),
                "target_text": "重复。",
            },
            headers=HEADERS,
        ).status_code
        == 409
    )


@pytest.mark.parametrize("state", ["unknown", "reserved"])
def test_unknown_or_inflight_model_call_cannot_be_manually_retried(
    client, db, project, owner, state
):
    task_id = running_task(db, owner, project)
    task = db.get(Task, task_id)
    task.status, task.error = "failed", "model_unknown"
    day = datetime.now(UTC).date()
    db.add(BudgetDay(day=day, spent_usd=0, reserved_usd=Decimal("0.01")))
    db.flush()
    db.add(
        ModelCall(
            task_id=task_id,
            user_id=owner,
            budget_day=day,
            state=state,
            reserved_usd=Decimal("0.01"),
        )
    )
    db.commit()
    assert client.post(f"/api/tasks/{task_id}/retry", headers=HEADERS).status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_manual_retry_preserves_original_task_cost_and_uses_original_work(
    client, db, project, chapter, owner
):
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="旧的结尾。"
    )
    task = db.get(Task, task_id)
    task.status, task.error = "failed", "model_failed"
    task.input_snapshot = {"constraints": "旧约束"}
    day = datetime.now(UTC).date()
    db.add(BudgetDay(day=day, spent_usd=Decimal("0.01"), reserved_usd=0))
    db.flush()
    db.add(
        ModelCall(
            task_id=task_id,
            user_id=owner,
            budget_day=day,
            state="completed",
            reserved_usd=0,
            charged_usd=Decimal("0.01"),
        )
    )
    work = db.get(Project, UUID(project["id"]))
    work.is_current, work.completed, work.constraints = False, True, "已保存的新约束"
    stale_activity = datetime.now(UTC) - timedelta(days=2)
    db.get(Account, owner).last_active_at = stale_activity
    db.commit()
    response = client.post(f"/api/tasks/{task_id}/retry", headers=HEADERS)
    assert response.status_code == 201, response.text
    new = response.json()
    assert new["id"] != str(task_id) and new["retry_of"] == str(task_id)
    assert new["project_id"] == project["id"] and new["chapter_id"] == str(chapter)
    assert new["source"] == "web" and new["target_text"] == "旧的结尾。"
    db.expire_all()
    assert db.get(Task, UUID(new["id"])).input_snapshot is None
    assert db.get(Task, task_id).status == "failed"
    assert db.get(Account, owner).last_active_at == stale_activity
    assert db.scalar(select(ModelCall.charged_usd).where(ModelCall.task_id == task_id)) == Decimal(
        "0.01"
    )


def test_changed_fragment_rejected_without_partial_adoption(db, client, project, chapter, owner):
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="旧的结尾。"
    )
    client.put(
        f"/api/chapters/{chapter}",
        json={"title": "第一章", "content": document("已手动替换原片段。")},
        headers=HEADERS,
    )
    db.expire_all()
    with pytest.raises(HTTPException):
        adopt_result(db, db.get(Task, task_id), RESULT)
    db.rollback()
    assert client.get(f"/api/chapters/{chapter}").json()["text"] == "已手动替换原片段。"


def test_concurrent_adoption_produces_one_chapter_and_version(db, engine, owner, project):
    task_id = running_task(db, owner, project)
    barrier = Barrier(2)

    def apply():
        with Session(engine) as session:
            task = session.get(Task, task_id)
            barrier.wait(timeout=5)
            result = adopt_result(session, task, RESULT)
            chapter_id = result.id
            session.commit()
            return chapter_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert results[0] == results[1]
    assert db.scalar(select(func.count()).select_from(Chapter)) == 1
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1


def test_rewrite_preserves_unselected_rich_text_nodes(db, client, owner, project, chapter):
    before = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "保留标题"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "开头", "marks": [{"type": "bold"}]},
                    {"type": "text", "text": "旧的结尾。"},
                    {"type": "text", "text": "后文", "marks": [{"type": "italic"}]},
                ],
            },
        ],
    }
    assert (
        client.put(
            f"/api/chapters/{chapter}", json={"title": "第一章", "content": before}, headers=HEADERS
        ).status_code
        == 200
    )
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="旧的结尾。"
    )
    adopt_result(db, db.get(Task, task_id), {**RESULT, "paragraphs": ["新的结尾。"]})
    db.commit()
    after = client.get(f"/api/chapters/{chapter}").json()["content"]
    assert after["content"][0] == before["content"][0]
    assert after["content"][1]["content"][0] == before["content"][1]["content"][0]
    assert after["content"][1]["content"][-1] == before["content"][1]["content"][-1]


def test_rewrite_across_paragraphs_preserves_outside_marks_and_breaks(
    db, client, owner, project, chapter
):
    prefix = [
        {"type": "text", "text": "Before", "marks": [{"type": "bold"}]},
        {"type": "hardBreak"},
    ]
    suffix = [
        {"type": "hardBreak"},
        {"type": "text", "text": "After", "marks": [{"type": "italic"}]},
    ]
    before = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": prefix + [{"type": "text", "text": "old one"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "old two"}] + suffix},
        ],
    }
    assert (
        client.put(
            f"/api/chapters/{chapter}",
            json={"title": "First", "content": before},
            headers=HEADERS,
        ).status_code
        == 200
    )
    task_id = running_task(
        db, owner, project, kind="rewrite", chapter_id=chapter, target_text="old one\nold two"
    )
    adopt_result(db, db.get(Task, task_id), {**RESULT, "paragraphs": ["new one", "new two"]})
    db.commit()
    after = client.get(f"/api/chapters/{chapter}").json()
    assert after["text"] == "Before\nnew one\nnew two\nAfter"
    assert after["content"]["content"][0]["content"][:2] == prefix
    assert after["content"]["content"][-1]["content"][-2:] == suffix


def test_unsupported_rewrite_block_rejected_before_task_creation(client, db, project, chapter):
    content = {
        "type": "doc",
        "content": [{"type": "blockquote", "content": document("Quoted target")["content"]}],
    }
    assert (
        client.put(
            f"/api/chapters/{chapter}",
            json={"title": "First", "content": content},
            headers=HEADERS,
        ).status_code
        == 200
    )
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "kind": "rewrite",
            "goal": "Rewrite only the target",
            "chapter_id": str(chapter),
            "target_text": "Quoted target",
        },
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_work_summaries_omit_materials_and_body_but_retain_versions(client, project, chapter):
    projects = client.get("/api/projects?summary=true")
    assert projects.status_code == 200
    assert set(projects.json()[0]) == {
        "id", "title", "kind", "is_current", "completed", "index_revision", "material_revision",
        "cover_image_id",
    }
    chapters = client.get(f"/api/projects/{project['id']}/chapters?summary=true")
    assert chapters.status_code == 200
    assert set(chapters.json()[0]) == {"id", "title", "position", "version"}
    assert chapters.json()[0]["id"] == str(chapter)
    assert client.get(f"/api/chapters/{chapter}").json()["content"]
    revision = projects.json()[0]["material_revision"]
    client.put(f"/api/projects/{project['id']}", json={"summary": "新资料"}, headers=HEADERS)
    assert client.get("/api/projects?summary=true").json()[0]["material_revision"] != revision


def test_task_list_reads_model_ledger_once(client, db, owner, project):
    tasks = [Task(user_id=owner, project_id=UUID(project["id"]), kind="scene",
                  status="failed", source="web") for _ in range(12)]
    db.add_all(tasks)
    day = datetime.now(UTC).date()
    db.add(BudgetDay(day=day, spent_usd=Decimal("0.02"), reserved_usd=Decimal("0.01")))
    db.flush()
    db.add_all([
        ModelCall(task_id=tasks[0].id, user_id=owner, budget_day=day, state="reserved",
                  reserved_usd=Decimal("0.01")),
        ModelCall(task_id=tasks[1].id, user_id=owner, budget_day=day, state="succeeded",
                  reserved_usd=Decimal("0.03"), charged_usd=Decimal("0.02"),
                  input_tokens=12, output_tokens=8),
    ])
    first, second = str(tasks[0].id), str(tasks[1].id)
    db.commit()
    queries = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if "FROM model_calls" in statement:
            queries.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(f"/api/projects/{project['id']}/tasks")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert len(response.json()) == 12
    items = {item["id"]: item for item in response.json()}
    assert not items[first]["can_retry"]
    assert Decimal(items[first]["reserved_usd"]) == Decimal("0.01")
    assert items[second]["can_retry"] and items[second]["input_tokens"] == 12
    assert Decimal(items[second]["charged_usd"]) == Decimal("0.02")
    assert all(item["can_retry"] and item["input_tokens"] == 0
               for key, item in items.items() if key not in {first, second})
    assert len(queries) == 1
