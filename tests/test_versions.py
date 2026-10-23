from datetime import UTC, datetime, timedelta
from urllib.parse import unquote
from uuid import UUID

import pytest
from sqlalchemy import select
from test_retrieval import index_all
from test_retrieval import vector_settings as vector_settings
from test_works import HEADERS, document
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.models import Account, Chapter, ChapterVersion, Memory, ModelCall, Project, Task
from lisa.retrieval import recall


def save(client, chapter_id, title="新版标题", content=None):
    response = client.put(
        f"/api/chapters/{chapter_id}",
        json={"title": title, "content": content or document("这是新版正文。")},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()


def test_versions_list_is_metadata_and_detail_retains_formal_content(client, db, chapter):
    save(client, chapter)
    versions = client.get(f"/api/chapters/{chapter}/versions").json()
    assert [item["version"] for item in versions] == [2, 1]
    assert set(versions[0]) == {"title", "version", "source", "created_at"}
    old = client.get(f"/api/chapters/{chapter}/versions/1").json()
    assert old["title"] == "第一章" and old["text"] == "开头保留。旧的结尾。后文保留。"
    assert old["content"] == document(old["text"])
    assert client.get(f"/api/chapters/{chapter}/versions/100").status_code == 404
    assert client.get(f"/api/chapters/{chapter}/versions/0").status_code == 404


def test_restore_creates_current_version_without_changing_other_state(
    client, db, owner, project, chapter
):
    old_content = db.get(Chapter, chapter).content
    save(client, chapter)
    work = db.get(Project, UUID(project["id"]))
    work.completed, work.is_current = True, False
    work.summary, work.plot_state = "另行维护的摘要", "另行维护的剧情状态"
    user = db.get(Account, owner)
    user.last_active_at = datetime.now(UTC) - timedelta(days=2)
    activity = user.last_active_at
    new = Project(user_id=owner, title="当前另一部", is_current=True)
    memory = Memory(user_id=owner, content="原生活记录", kind="life_event", active=False)
    db.add_all([new, memory])
    db.commit()
    response = client.post(f"/api/chapters/{chapter}/versions/1/restore", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["version"] == 3 and response.json()["content"] == old_content
    db.expire_all()
    versions = list(
        db.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter)
            .order_by(ChapterVersion.version)
        )
    )
    assert [item.version for item in versions] == [1, 2, 3]
    assert versions[-1].source == "restore:1" and versions[1].content == document("这是新版正文。")
    assert work.completed and not work.is_current and new.is_current
    assert work.summary == "另行维护的摘要" and work.plot_state == "另行维护的剧情状态"
    assert not memory.active and memory.content == "原生活记录"
    assert user.last_active_at == activity
    assert not list(db.scalars(select(Task))) and not list(db.scalars(select(ModelCall)))
    # Restoring the same content again is a no-op, like an unchanged manual save.
    assert (
        client.post(f"/api/chapters/{chapter}/versions/1/restore", headers=HEADERS).json()[
            "version"
        ]
        == 3
    )
    assert len(client.get(f"/api/chapters/{chapter}/versions").json()) == 3


@pytest.mark.asyncio
async def test_restore_invalidates_vector_and_only_new_current_body_is_reindexed(
    client, db, engine, vector_settings, owner, project, chapter
):
    save(client, chapter)
    await index_all(engine, vector_settings)
    project_id = UUID(project["id"])
    assert "新版正文" in recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0]).text
    restored = client.post(f"/api/chapters/{chapter}/versions/1/restore", headers=HEADERS)
    assert restored.status_code == 200
    db.expire_all()
    assert not recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0]).text
    await index_all(engine, vector_settings)
    text = recall(db, vector_settings, owner, project_id, "writing", [1, 0, 0]).text
    assert "旧的结尾" in text and "新版正文" not in text


def test_export_contains_only_ordered_current_body_and_safe_download_name(
    client, db, owner, project, chapter
):
    work = db.get(Project, UUID(project["id"]))
    work.title = 'CON:雨/信"\r\n'
    work.worldbook, work.author_prompt, work.characters = "秘密设定", "秘密指令", "秘密人物配置"
    db.commit()
    save(
        client,
        chapter,
        content={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "当前一", "marks": [{"type": "bold"}]},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "换行"},
                    ],
                }
            ],
        },
    )
    db.add(Chapter(project_id=work.id, title="第二章", position=2, content=document("当前二")))
    db.commit()
    response = client.get(f"/api/projects/{work.id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    name = unquote(response.headers["content-disposition"].split("UTF-8''")[1])
    assert name.startswith("LISA-CON_") and name.endswith(".txt")
    assert all(char not in name for char in '<>:"/\\|?*\r\n')
    assert response.text.index("新版标题") < response.text.index("第二章")
    assert "当前一\n换行" in response.text and "当前二" in response.text
    assert all(
        text not in response.text
        for text in ("秘密设定", "秘密指令", "秘密人物配置", "旧的结尾", "source", "version")
    )


def test_empty_work_cannot_export(client, project):
    assert client.get(f"/api/projects/{project['id']}/export").status_code == 409


def test_version_and_export_access_do_not_cross_accounts(client, db, owner, chapter):
    foreign = Account(username="foreign_versions", password_hash="synthetic")
    db.add(foreign)
    db.flush()
    work = Project(user_id=foreign.id, title="别人的正文")
    db.add(work)
    db.flush()
    item = Chapter(project_id=work.id, title="保密章节", position=1, content=document("保密正文"))
    db.add(item)
    db.flush()
    db.add(
        ChapterVersion(
            chapter_id=item.id, version=1, title=item.title, content=item.content, source="manual"
        )
    )
    db.commit()
    for path in (
        f"/api/chapters/{item.id}/versions",
        f"/api/chapters/{item.id}/versions/1",
        f"/api/projects/{work.id}/export",
    ):
        assert client.get(path).status_code == 404
    assert (
        client.post(f"/api/chapters/{item.id}/versions/1/restore", headers=HEADERS).status_code
        == 404
    )
    assert (
        client.post(f"/api/chapters/{chapter}/versions/100/restore", headers=HEADERS).status_code
        == 404
    )
    assert client.post(f"/api/chapters/{chapter}/versions/1/restore").status_code == 403
