from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_retrieval import index_all, memory
from test_retrieval import library as library
from test_retrieval import vector_settings as vector_settings
from test_works import HEADERS, document
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.models import Account, Chapter, Project, RetrievalReference, Task
from lisa.recall import retrieve_context
from lisa.retrieval import Context, recall, record_references

DEFAULTS = {"sources": ["memory", "project", "chapter"], "limit": 5, "max_chars": 1500}


def task_for(db, owner, project_id):
    task = Task(user_id=owner, project_id=project_id, kind="scene", status="completed")
    db.add(task)
    db.flush()
    return task


def add_reference(db, task, kind, source_id, *, revision=1, start=0, end=5):
    position = len(task_references(db, task.id))
    db.add(
        RetrievalReference(
            task_id=task.id,
            position=position,
            source_kind=kind,
            source_id=source_id,
            revision=revision,
            space="previous-embedding-space",
            chunk_index=0,
            start=start,
            end=end,
        )
    )
    db.flush()


def task_references(db, task_id):
    return db.scalars(select(RetrievalReference).where(RetrievalReference.task_id == task_id)).all()


def test_config_save_restore_and_new_work_do_not_change_activity_or_sources(
    client, db, owner, project
):
    project_id = UUID(project["id"])
    user = db.get(Account, owner)
    work = db.get(Project, project_id)
    previous_activity = datetime.now(UTC) - timedelta(days=2)
    user.last_active_at = previous_activity
    before = (user.memory_revision, work.index_revision)
    db.commit()
    path = f"/api/projects/{project_id}/retrieval"
    result = client.get(path)
    assert result.status_code == 200
    assert result.json()["config"] == result.json()["defaults"] == DEFAULTS
    assert isinstance(result.json()["enabled"], bool)

    custom = {"sources": [], "limit": 12, "max_chars": 6000}
    assert client.put(path, json=custom, headers=HEADERS).json()["config"] == custom
    assert client.get(path).json()["config"] == custom
    db.refresh(user)
    db.refresh(work)
    assert (user.memory_revision, work.index_revision) == before
    assert user.last_active_at == previous_activity

    created = client.post(
        "/api/projects", json={"title": "另一部独立作品", "kind": "novel"}, headers=HEADERS
    )
    assert created.status_code == 201
    assert (
        client.get(f"/api/projects/{created.json()['id']}/retrieval").json()["config"] == DEFAULTS
    )
    assert client.put(path, json={}, headers=HEADERS).json()["config"] == DEFAULTS
    assert client.get(path).json()["config"] == DEFAULTS


@pytest.mark.parametrize(
    "invalid",
    [
        {"limit": 13},
        {"limit": True},
        {"sources": ["chat"]},
        {"sources": ["memory", "memory"]},
        {"user_id": "untrusted"},
    ],
)
def test_invalid_config_does_not_overwrite_saved_values(client, project, invalid):
    path = f"/api/projects/{project['id']}/retrieval"
    saved = {"sources": ["chapter"], "limit": 2, "max_chars": 700}
    assert client.put(path, json=saved, headers=HEADERS).status_code == 200
    assert client.put(path, json=invalid, headers=HEADERS).status_code == 422
    assert client.get(path).json()["config"] == saved


def test_config_and_reference_routes_hide_other_accounts(client, db, owner, project):
    foreign = Account(username="foreign_retrieval_config", password_hash="synthetic")
    db.add(foreign)
    db.flush()
    work = Project(user_id=foreign.id, title="别人的作品")
    db.add(work)
    db.flush()
    task = task_for(db, foreign.id, work.id)
    db.commit()
    assert client.get(f"/api/projects/{work.id}/retrieval").status_code == 404
    assert (
        client.put(f"/api/projects/{work.id}/retrieval", json={}, headers=HEADERS).status_code
        == 404
    )
    assert client.get(f"/api/tasks/{task.id}/references").status_code == 404
    assert client.put(f"/api/projects/{project['id']}/retrieval", json={}).status_code == 403


@pytest.mark.asyncio
async def test_source_filter_applies_before_nearest_result_limit(
    db, engine, vector_settings, library
):
    user_id, _, project_id, _, _ = library
    memory(db, user_id, "最近：排在最前但未选择的记忆", allow_writing=True)
    work = db.get(Project, project_id)
    work.worldbook = "最近：排在最前但未选择的世界书"
    body = Chapter(
        project_id=project_id, title="正文", position=1, content=document("允许召回的前文")
    )
    db.add(body)
    db.commit()
    await index_all(engine, vector_settings)
    result = recall(
        db,
        vector_settings,
        user_id,
        project_id,
        "writing",
        [1, 0, 0],
        sources=["chapter"],
        limit=1,
    )
    assert len(result.references) == 1
    assert result.references[0]["source_kind"] == "chapter"
    assert "允许召回的前文" in result.text
    assert "未选择" not in result.text


@pytest.mark.asyncio
async def test_empty_author_sources_skip_embedding_while_chat_still_recalls(
    db, engine, vector_settings, library
):
    user_id, _, project_id, _, _ = library
    memory(db, user_id, "最近：聊天仍可使用的记忆", allow_writing=True)
    work = db.get(Project, project_id)
    work.retrieval_config = {**DEFAULTS, "sources": []}
    task = task_for(db, user_id, project_id)
    db.commit()
    await index_all(engine, vector_settings)
    calls = []

    async def embed(*args):
        calls.append(args[-1])
        return [[1, 0, 0]]

    assert (
        await retrieve_context(
            vector_settings, engine, task.id, user_id, project_id, "writing", "查询", embed=embed
        )
        == Context()
    )
    assert calls == []
    result = await retrieve_context(
        vector_settings, engine, task.id, user_id, project_id, "chat", "查询", embed=embed
    )
    assert len(calls) == 1
    assert "聊天仍可使用的记忆" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [[], ["project"]])
async def test_config_saved_during_embedding_controls_returned_context(
    db, engine, vector_settings, library, sources
):
    user_id, _, project_id, _, _ = library
    memory(db, user_id, "最近：查询发出时允许的记忆", allow_writing=True)
    work = db.get(Project, project_id)
    work.worldbook = "查询返回时选择的世界书" * 100
    work.retrieval_config = {**DEFAULTS, "sources": ["memory"]}
    task = task_for(db, user_id, project_id)
    db.commit()
    await index_all(engine, vector_settings)

    async def embed(*args):
        with Session(engine) as current, current.begin():
            current.get(Project, project_id).retrieval_config = {
                "sources": sources,
                "limit": 1,
                "max_chars": 300,
            }
        return [[1, 0, 0]]

    result = await retrieve_context(
        vector_settings, engine, task.id, user_id, project_id, "writing", "查询", embed=embed
    )
    if not sources:
        assert result == Context()
    else:
        assert len(result.references) == 1
        assert result.references[0]["source_kind"] == "project"
        assert "选择的世界书" in result.text
        assert len(result.text) <= 300
    assert "允许的记忆" not in result.text


@pytest.mark.asyncio
async def test_reference_reads_complete_memory_after_config_and_space_change(
    client, db, engine, vector_settings, owner, project
):
    project_id = UUID(project["id"])
    item = memory(db, owner, "句首。" + "可追溯的资料。" * 120, allow_writing=True)
    task = task_for(db, owner, project_id)
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(
        db,
        vector_settings,
        owner,
        project_id,
        "writing",
        [1, 0, 0],
        max_chars=1500,
        limit=1,
        sources=["memory"],
    )
    assert len(context.references) == 1
    record_references(db, task.id, context)
    db.commit()
    ref = context.references[0]
    assert ref["start"] == 0 and ref["end"] == len(item.content)
    path = f"/api/tasks/{task.id}/references"
    before = client.get(path)
    assert before.status_code == 200
    row = before.json()["references"][0]
    assert row["state"] == "available"
    assert row["text"] == item.content[ref["start"] : ref["end"]]
    assert row["text"] in context.text

    assert (
        client.put(
            f"/api/projects/{project_id}/retrieval", json={"sources": []}, headers=HEADERS
        ).status_code
        == 200
    )
    settings_before = client.app.state.settings
    client.app.state.settings = vector_settings.model_copy(update={"embedding_model": "changed"})
    try:
        assert client.get(path).json() == before.json()
    finally:
        client.app.state.settings = settings_before


@pytest.mark.parametrize("change", ["revision", "deleted", "usage"])
def test_reference_hides_changed_deleted_and_disallowed_memory(client, db, owner, project, change):
    item = memory(db, owner, "必须停止显示的旧内容", allow_writing=True)
    task = task_for(db, owner, UUID(project["id"]))
    add_reference(db, task, "memory", item.id, end=len(item.content))
    db.commit()
    path = f"/api/tasks/{task.id}/references"
    assert client.get(path).json()["references"][0]["text"] == item.content
    if change == "revision":
        item.content = "修改后的新内容"
        item.revision += 1
    elif change == "deleted":
        item.active = False
    else:
        item.allow_writing = False
    db.commit()
    row = client.get(path).json()["references"][0]
    assert row["state"] == ("changed" if change == "revision" else "unavailable")
    assert row["text"] is None and row["title"] is None


def test_references_recheck_current_chapter_version_and_scope(client, db, owner, project, chapter):
    project_id = UUID(project["id"])
    other = Project(user_id=owner, title="同用户另一作品的隐秘标题", is_current=False)
    foreign_user = Account(username="reference_injected_user", password_hash="synthetic")
    db.add_all([other, foreign_user])
    db.flush()
    foreign_memory = memory(db, foreign_user.id, "其他账号秘密", allow_writing=True)
    foreign_project = Project(user_id=foreign_user.id, title="其他账号作品")
    db.add(foreign_project)
    db.flush()
    other.worldbook = "同用户另一作品的秘密"
    other_memory = memory(db, owner, "另一作品专属记忆", project_id=other.id, allow_writing=True)
    other_chapter = Chapter(
        project_id=other.id, title="另一作品章节", position=1, content=document("另一作品正文")
    )
    db.add(other_chapter)
    db.flush()
    task = task_for(db, owner, project_id)
    add_reference(db, task, "chapter", chapter, end=4)
    add_reference(db, task, "project", other.id)
    add_reference(db, task, "memory", other_memory.id)
    add_reference(db, task, "chapter", other_chapter.id)
    add_reference(db, task, "memory", foreign_memory.id)
    add_reference(db, task, "project", foreign_project.id)
    db.commit()
    path = f"/api/tasks/{task.id}/references"
    rows = client.get(path).json()["references"]
    assert rows[0]["state"] == "available" and rows[0]["text"] == "开头保留"
    assert all(row["state"] == "unavailable" and row["text"] is None for row in rows[1:])
    assert all(row["title"] is None for row in rows[1:])
    body = db.get(Chapter, chapter)
    body.content, body.version = document("替换为新版正文"), 2
    db.commit()
    row = client.get(path).json()["references"][0]
    assert row["state"] == "changed" and row["text"] is None and row["title"] is None


def test_character_references_use_live_owned_knowledge_and_revision(client, db, owner, project):
    from lisa.models import Character

    role = Character(
        project_id=UUID(project["id"]),
        name="旅人",
        prompt="自己的视角",
        known_information="我见过那封信。",
        enabled=True,
    )
    db.add(role)
    db.flush()
    task = task_for(db, owner, role.project_id)
    add_reference(db, task, "character", role.id, end=8)
    db.commit()
    path = f"/api/tasks/{task.id}/references"
    item = client.get(path).json()["references"][0]
    assert item["state"] == "available" and item["title"] == "旅人"
    assert item["text"] == role.known_information[:8]
    role.known_information = "我没有见过那封信。"
    role.revision += 1
    db.commit()
    changed = client.get(path).json()["references"][0]
    assert changed["state"] == "changed" and changed["text"] is None
    role.enabled = False
    db.commit()
    assert client.get(path).json()["references"][0]["state"] == "unavailable"
    db.delete(role)
    db.commit()
    assert client.get(path).json()["references"][0]["text"] is None
