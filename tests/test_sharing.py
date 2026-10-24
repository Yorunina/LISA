from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_works import HEADERS, RESULT, document
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.app import create_app
from lisa.author import execute_author
from lisa.chat import execute_chat, ingest_message
from lisa.models import Account, Chapter, OutboundMessage, Project, Task
from lisa.works import create_writing_task


def test_public_reader_is_scoped_saved_escaped_and_revocable(
    client, settings, db, owner, project, chapter
):
    project_id = UUID(project["id"])
    work = db.get(Project, project_id)
    work.worldbook = "PRIVATE_WORLD"
    work.summary = "PRIVATE_SUMMARY"
    saved = db.get(Chapter, chapter)
    saved.content = document('<script>alert("x")</script> 已保存正文')
    other_work = Project(user_id=owner, title="不可见的另一本", is_current=False)
    db.add(other_work)
    db.flush()
    other_chapter = Chapter(
        project_id=other_work.id, title="秘密章节", position=1, content=document("PRIVATE_OTHER")
    )
    db.add(other_chapter)
    db.commit()
    result = client.post(f"/api/projects/{project_id}/share", json={}, headers=HEADERS)
    assert result.status_code == 200
    url = result.json()["url"]
    assert url.startswith(settings.web_url + "/read/")
    assert len(url.rsplit("/", 1)[1]) == 43
    assert client.post(
        f"/api/projects/{project_id}/share", json={}, headers=HEADERS
    ).json()["url"] == url
    with TestClient(create_app(settings)) as anonymous:
        response = anonymous.get(url + f"?chapter={chapter}")
        assert response.status_code == 200
        assert "&lt;script&gt;" in response.text and "<script>" not in response.text
        assert "已保存正文" in response.text
        assert all(text not in response.text for text in (
            "PRIVATE_WORLD", "PRIVATE_SUMMARY", "PRIVATE_OTHER", "不可见的另一本"
        ))
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "noindex" in response.headers["X-Robots-Tag"]
        assert anonymous.get(url + f"?chapter={other_chapter.id}").status_code == 404
        assert anonymous.get(f"/api/chapters/{chapter}").status_code == 401
        assert anonymous.post(
            f"/api/projects/{project_id}/share", json={}, headers=HEADERS
        ).status_code == 401
        saved.content = document("后来保存的正文")
        db.commit()
        assert "后来保存的正文" in anonymous.get(url).text
        revoked = client.delete(f"/api/projects/{project_id}/share", headers=HEADERS)
        assert revoked.status_code == 204
        assert anonymous.get(url).status_code == 404
        new_url = client.post(
            f"/api/projects/{project_id}/share", json={}, headers=HEADERS
        ).json()["url"]
        assert new_url != url
        assert anonymous.get(url).status_code == 404
        assert anonymous.get(new_url).status_code == 200
        db.get(Account, owner).active = False
        db.commit()
        assert anonymous.get(new_url).status_code == 404
        db.execute(delete(Account).where(Account.id == owner))
        db.commit()
        assert anonymous.get(new_url).status_code == 404


def test_share_creation_and_revocation_enforce_owner(client, db, owner):
    other = Account(username="other_sharer", password_hash="synthetic")
    db.add(other)
    db.flush()
    work = Project(user_id=other.id, title="别人的作品", share_token="x" * 43)
    db.add(work)
    db.commit()
    created = client.post(f"/api/projects/{work.id}/share", json={}, headers=HEADERS)
    assert created.status_code == 404
    assert client.delete(f"/api/projects/{work.id}/share", headers=HEADERS).status_code == 404
    db.refresh(work)
    assert work.share_token == "x" * 43


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["own", "other", "missing"])
async def test_chat_can_share_saved_chapter_without_approval(
    client, settings, engine, db, owner, project, chapter, selection
):
    chosen = chapter
    if selection == "missing":
        chosen = uuid4()
    elif selection == "other":
        other = Account(username="other_agent_reader", password_hash="synthetic")
        db.add(other)
        db.flush()
        work = Project(user_id=other.id, title="另一账号的作品")
        db.add(work)
        db.flush()
        foreign = Chapter(
            project_id=work.id, title="不可分享", position=1, content=document("秘密")
        )
        db.add(foreign)
        db.flush()
        chosen = foreign.id
    task_id = ingest_message(
        db, bot_id="synthetic", message_id="share", qq_id="720000001",
        sent_at=datetime.now(UTC), content="今天怎么样",
    )
    db.commit()

    async def generate(*args):
        assert str(chapter) in args[4]
        assert "无需征得用户同意" in args[4]
        return {"reply": "有一段故事想给你看看。", "opinions": [], "commission": None,
                "share_chapter_id": str(chosen)}

    await execute_chat(settings, engine, task_id, generate=generate)
    db.expire_all()
    message = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert db.get(Task, task_id).status == "completed"
    assert "旧的结尾" not in message.content
    work = db.get(Project, UUID(project["id"]))
    if selection == "own":
        assert work.share_token
        link = message.content.splitlines()[-1]
        assert f"?chapter={chapter}" in link
        assert client.get(link).status_code == 200
    else:
        assert "/read/" not in message.content
        assert work.share_token is None


@pytest.mark.asyncio
async def test_qq_author_completion_links_to_saved_chapter(
    settings, engine, client, db, owner, project
):
    work = db.get(Project, UUID(project["id"]))
    work.agreement = "一个雨中发现信的场景"
    task = create_writing_task(
        db, db.get(Account, owner), work, "scene", "发现信", source="qq", notify_qq=True
    )
    task_id = task.id
    db.commit()

    async def generate(*args):
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed"
    message = db.scalar(select(OutboundMessage).where(OutboundMessage.task_id == task_id))
    assert RESULT["paragraphs"][0] not in message.content
    assert f"?chapter={task.chapter_id}" in message.content
    assert RESULT["paragraphs"][0] in client.get(message.content.splitlines()[-1]).text
