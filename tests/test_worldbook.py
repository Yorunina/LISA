import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from test_author import pending_task
from test_retrieval import index_all
from test_retrieval import library as library
from test_retrieval import vector_settings as vector_settings
from test_works import HEADERS, RESULT
from test_works import owner as owner
from test_works import project as project

from lisa.author import execute_author
from lisa.models import Memory, Project
from lisa.recall import retrieve_context
from lisa.retrieval import recall, recall_titles


def entry(title="北境火术禁令", content="只有持证医师可以使用火术。", **values):
    return {
        "id": str(uuid4()),
        "title": title,
        "category": "rule",
        "content": content,
        "scope": "旧历320年后，仅在北境生效",
        "related_ids": [],
        **values,
    }


def test_save_preserves_core_validates_links_and_invalidates_only_changed_source(
    client, db, owner, project
):
    url = f"/api/projects/{project['id']}"
    base = entry("火术规则", "火术会消耗施术者体温。")
    local = entry(related_ids=[base["id"]])
    data = {"worldbook": "时间不能逆转。", "worldbook_entries": [base, local]}
    response = client.put(url, json=data, headers=HEADERS)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["worldbook"] == "时间不能逆转。"
    assert saved["worldbook_entries"] == data["worldbook_entries"]
    revision = saved["index_revision"]
    assert client.put(url, json=data, headers=HEADERS).json()["index_revision"] == revision
    state = client.put(url, json={"plot_state": "当前位于北境。"}, headers=HEADERS).json()
    assert state["index_revision"] == revision
    for bad in ([base, base], [local], [entry(related_ids=[local["id"]])], [entry(title=" ")]):
        assert client.put(url, json={"worldbook_entries": bad}, headers=HEADERS).status_code == 422
    assert client.get(url).json()["worldbook_entries"] == data["worldbook_entries"]
    assert (
        client.put(url, json={"worldbook_entries": []}, headers=HEADERS).json()["worldbook"]
        == data["worldbook"]
    )
    # An arbitrary project id is never writable through this account.
    assert client.put(f"/api/projects/{uuid4()}", json=data, headers=HEADERS).status_code == 404


@pytest.mark.asyncio
async def test_title_recall_without_vectors_is_current_scoped_and_obeys_config(
    settings, engine, db, library
):
    user_id, other, project_id, another, foreign = library
    base = entry("火术规则", "火术会消耗施术者体温。")
    local = entry(related_ids=[base["id"]])
    work = db.get(Project, project_id)
    work.worldbook_entries = [base, local, entry("秘密组织", "无关秘密不进入上下文。")]
    db.get(Project, another).worldbook_entries = [entry(content="另一作品的秘密")]
    db.get(Project, foreign).worldbook_entries = [entry(content="另一用户的秘密")]
    db.commit()
    embed = AsyncMock(side_effect=AssertionError("No model calls in title fallback"))
    result = await retrieve_context(
        settings, engine, uuid4(), user_id, project_id, "writing", "描写北境火术禁令", embed=embed
    )
    assert "持证医师" in result.text and "消耗施术者体温" in result.text
    assert "旧历320年" in result.text and len(result.references) == 2
    assert "无关秘密" not in result.text and "另一" not in result.text
    assert not recall_titles(db, other, project_id, "writing", local["title"]).text
    assert not recall_titles(db, user_id, project_id, "role", local["title"]).text
    assert not recall_titles(db, user_id, project_id, "writing", local["title"], sources=[]).text
    work.worldbook_entries = [base, {**local, "content": "禁令已经解除。"}]
    work.index_revision += 1
    db.commit()
    assert "解除" in recall_titles(db, user_id, project_id, "writing", local["title"]).text
    work.retrieval_config = {"sources": [], "limit": 5, "max_chars": 1500}
    db.commit()
    assert not (
        await retrieve_context(
            settings, engine, uuid4(), user_id, project_id, "writing", local["title"], embed=embed
        )
    ).text


@pytest.mark.asyncio
async def test_semantic_entry_is_complete_with_dependencies_and_no_unrelated_text(
    engine, db, vector_settings, library
):
    user_id, _, project_id, _, _ = library
    base = entry("通用火术", "施术必须消耗体温。")
    selected = entry(
        "最近：禁令", "规则正文。" * 160 + "末尾例外：持证医师不受限制。", related_ids=[base["id"]]
    )
    work = db.get(Project, project_id)
    work.worldbook_entries = [base, selected, entry("隐藏设定", "绝对不能混入的无关内容。")]
    db.commit()
    await index_all(engine, vector_settings)
    context = recall(
        db, vector_settings, user_id, project_id, "writing", [1, 0, 0], max_chars=4000, limit=2
    )
    assert "末尾例外：持证医师不受限制。" in context.text
    assert "必须消耗体温" in context.text
    assert "绝对不能混入" not in context.text
    assert len(context.references) == 2
    short = recall_titles(db, user_id, project_id, "writing", selected["title"], max_chars=300)
    assert "预算不足" in short.text and not short.references
    assert "规则正文" not in short.text
    work.worldbook_entries = []
    work.index_revision += 1
    db.commit()
    assert not recall(db, vector_settings, user_id, project_id, "writing", [1, 0, 0]).text


@pytest.mark.asyncio
async def test_author_passes_only_selected_entries_and_references_show_title(
    settings, engine, db, client, owner, project
):
    work = db.get(Project, UUID(project["id"]))
    work.worldbook = "不可穿越时间。"
    selected = entry()
    work.worldbook_entries = [selected, entry("其他地点", "不应全文注入的设定。")]
    db.add(Memory(
        user_id=owner, kind="fact", content="近期兜底不能挤占标题结果。", allow_writing=True
    ))
    task_id = pending_task(db, owner, project)
    with Session(engine) as current, current.begin():
        from lisa.models import Task

        current.get(Task, task_id).request = {"goal": "描写北境火术禁令下的治疗场景。"}

    async def generate(settings, engine, task_id, user_id, instructions, messages):
        payload = json.loads(messages[0]["content"])
        assert payload["worldbook"] == "不可穿越时间。"
        assert "worldbook_entries" not in payload
        assert "持证医师" in instructions and "旧历320年" in instructions
        assert "不应全文注入" not in instructions
        assert "近期兜底不能挤占" not in instructions
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    refs = client.get(f"/api/tasks/{task_id}/references").json()["references"]
    assert len(refs) == 1 and refs[0]["title"] == selected["title"]
    assert "持证医师" in refs[0]["text"]
    response = client.put(
        f"/api/projects/{project['id']}", json={"worldbook_entries": []}, headers=HEADERS
    )
    assert response.status_code == 200
    changed = client.get(f"/api/tasks/{task_id}/references").json()["references"][0]
    assert changed["state"] == "changed" and changed["text"] is None
