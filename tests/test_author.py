import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from procrastinate.exceptions import JobRetry
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_works import HEADERS, RESULT
from test_works import chapter as chapter
from test_works import owner as owner
from test_works import project as project

from lisa.author import _pack_author_messages, execute_author
from lisa.chat import ingest_message
from lisa.dialogue import Dialogue
from lisa.model_calls import BudgetExceeded, ModelBusy, ModelCallFailed, ModelCallUnknown
from lisa.models import Account, Chapter, ChapterVersion, OutboundMessage, Project, Task
from lisa.queue import create_queue
from lisa.works import create_writing_task


def pending_task(db, owner, project, *, kind="scene", chapter_id=None, source="web"):
    work = db.get(Project, UUID(project["id"]))
    work.agreement = "只写雨中发现信件的短场景。"
    user = db.get(Account, owner)
    user.last_active_at = datetime.now(UTC)
    task = create_writing_task(
        db,
        user,
        work,
        kind,
        "改变结尾但保留范围外正文",
        chapter_id=chapter_id,
        target_text="旧的结尾。" if kind == "rewrite" else None,
        source=source,
    )
    task_id = task.id
    db.commit()
    return task_id


def test_author_packet_omits_large_optional_material_after_required_state(
    settings,
):
    settings = settings.model_copy(update={"model_max_input_bytes": 11_000})
    messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "title": "作品",
                    "kind": "novel",
                    "goal": "写一个短场景",
                    "agreement": "保留当前人物关系。",
                    "constraints": "不要改变结局。",
                    "worldbook": "核心世界规则。",
                    "summary": "主角抵达车站。",
                    "plot_state": "正在等车。",
                    "context": "雨水落在站台边。",
                    "target_text": "",
                    "characters": "详细角色资料。" * 5000,
                    "preferences": "偏好。" * 500,
                    "tentative": "暂定。" * 500,
                },
                ensure_ascii=False,
            ),
        }
    ]
    packed, stats = _pack_author_messages(settings, "作者指令", messages)
    payload = json.loads(packed[0]["content"])
    assert payload["goal"] == "写一个短场景"
    assert "characters" not in payload
    assert "characters" in stats["omitted"]
    assert stats["request_bytes"] <= settings.model_max_input_bytes


@pytest.mark.asyncio
async def test_author_adopts_one_bounded_scene_with_saved_materials(
    settings, engine, db, owner, project
):
    work = db.get(Project, UUID(project["id"]))
    work.constraints = "主角不能死亡。"
    work.worldbook = "雨城没有电灯。"
    db.commit()
    task_id = pending_task(db, owner, project)
    calls = []

    async def generate(*args):
        calls.append(args)
        return RESULT

    await execute_author(settings, engine, task_id, generate=generate)
    await execute_author(settings, engine, task_id, generate=generate)
    assert len(calls) == 1
    payload = json.loads(calls[0][-1][0]["content"])
    assert payload["constraints"] == "主角不能死亡。"
    assert payload["worldbook"] == "雨城没有电灯。"
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed" and task.adopted_at is not None
    assert task.input_snapshot["messages"] == calls[0][-1]
    assert db.scalar(select(func.count()).select_from(Chapter)) == 1
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


@pytest.mark.asyncio
async def test_cancel_inflight_attempts_abort_and_discards_even_late_result(
    settings, engine, client, db, owner, project, chapter
):
    task_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)
    started, cancelled = asyncio.Event(), asyncio.Event()
    before = client.get(f"/api/chapters/{chapter}").json()

    async def generate(*args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return RESULT  # A remote request may finish despite local cancellation.

    running = asyncio.create_task(execute_author(settings, engine, task_id, generate=generate))
    await asyncio.wait_for(started.wait(), 2)
    response = client.post(f"/api/tasks/{task_id}/cancel", headers=HEADERS)
    assert response.status_code == 200
    await asyncio.wait_for(running, 2)
    assert cancelled.is_set()
    assert client.get(f"/api/chapters/{chapter}").json() == before
    db.expire_all()
    assert db.get(Task, task_id).status == "cancelled"
    assert db.get(Task, task_id).adopted_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BudgetExceeded, ModelCallFailed, ModelCallUnknown])
async def test_generation_failure_keeps_original_body_and_versions(
    settings, engine, client, db, owner, project, chapter, failure
):
    task_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)
    before = client.get(f"/api/chapters/{chapter}").json()
    materials_before = client.get(f"/api/projects/{project['id']}").json()

    async def generate(*args):
        raise failure("synthetic failure")

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"
    assert db.get(Task, task_id).adopted_at is None
    assert client.get(f"/api/chapters/{chapter}").json() == before
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1
    assert client.get(f"/api/projects/{project['id']}").json() == materials_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {**RESULT, "paragraphs": ["超" * 601]},
        {**RESULT, "paragraphs": ["  "]},
        {"title": "incomplete"},
    ],
)
async def test_invalid_or_overlong_result_is_not_adopted(
    settings, engine, db, owner, project, result
):
    task_id = pending_task(db, owner, project)

    async def generate(*args):
        return result

    await execute_author(
        settings.model_copy(update={"writing_max_chars": 600}), engine, task_id, generate=generate
    )
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["expired", "not_current", "completed", "no_agreement"])
async def test_autonomous_checks_start_time_not_enqueue_time(
    settings, engine, db, owner, project, restriction
):
    task_id = pending_task(db, owner, project, source="autonomous")
    work = db.get(Project, UUID(project["id"]))
    if restriction == "expired":
        db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(hours=24)
    elif restriction == "not_current":
        work.is_current = False
    elif restriction == "completed":
        work.completed = True
    else:
        work.agreement = ""
    db.commit()

    async def must_not_generate(*args):
        pytest.fail("Ineligible autonomous task reached the model")

    await execute_author(settings, engine, task_id, generate=must_not_generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0


@pytest.mark.asyncio
async def test_manual_rewrite_works_while_paused_old_and_completed_without_refreshing_activity(
    settings, engine, client, db, owner, project, chapter
):
    task_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)
    work = db.get(Project, UUID(project["id"]))
    work.is_current, work.completed, work.agreement = False, True, ""
    stale = datetime.now(UTC) - timedelta(days=2)
    db.get(Account, owner).last_active_at = stale
    db.commit()

    async def generate(*args):
        return {**RESULT, "paragraphs": ["新的结尾。"]}

    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(Account, owner).last_active_at == stale
    assert not db.get(Project, work.id).is_current and db.get(Project, work.id).completed
    assert client.get(f"/api/chapters/{chapter}").json()["text"] == "开头保留。新的结尾。后文保留。"


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["expired", "not_current"])
async def test_autonomous_waiting_for_model_slot_rechecks_start_conditions(
    settings, engine, db, owner, project, restriction
):
    task_id = pending_task(db, owner, project, source="autonomous")
    calls = []

    async def generate(*args):
        calls.append(args)
        raise ModelBusy()

    with pytest.raises(JobRetry):
        await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.started_at is None and task.input_snapshot is None
    if restriction == "expired":
        db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(days=2)
    else:
        db.get(Project, UUID(project["id"])).is_current = False
    db.commit()
    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "failed"
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0


@pytest.mark.asyncio
async def test_autonomous_waiting_for_model_slot_loads_latest_saved_materials(
    settings, engine, db, owner, project
):
    task_id = pending_task(db, owner, project, source="autonomous")
    snapshots = []

    async def generate(*args):
        snapshots.append(json.loads(args[-1][0]["content"]))
        if len(snapshots) == 1:
            raise ModelBusy()
        return RESULT

    with pytest.raises(JobRetry):
        await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    db.get(Project, UUID(project["id"])).constraints = "等待调用槽位期间保存的新约束"
    db.commit()
    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert snapshots[1]["constraints"] == "等待调用槽位期间保存的新约束"
    assert snapshots[0] != snapshots[1]


@pytest.mark.asyncio
async def test_inflight_autonomous_task_can_finish_bounded_result_after_pause(
    settings, engine, db, owner, project
):
    task_id = pending_task(db, owner, project, source="autonomous")
    started, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def generate(*args):
        snapshots.append(args[-1])
        started.set()
        await release.wait()
        return RESULT

    running = asyncio.create_task(execute_author(settings, engine, task_id, generate=generate))
    await asyncio.wait_for(started.wait(), 2)
    db.expire_all()
    work = db.get(Project, UUID(project["id"]))
    work.is_current, work.constraints = False, "不应用到正在生成任务的新约束"
    db.get(Account, owner).last_active_at = datetime.now(UTC) - timedelta(days=2)
    db.commit()
    release.set()
    await asyncio.wait_for(running, 2)
    await execute_author(settings, engine, task_id, generate=generate)
    db.expire_all()
    assert db.get(Task, task_id).status == "completed"
    assert db.get(Task, task_id).input_snapshot["messages"] == snapshots[0]
    assert len(snapshots) == 1
    assert db.scalar(select(func.count()).select_from(Chapter)) == 1
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == 1


@pytest.mark.asyncio
async def test_manual_retry_loads_latest_materials_but_preserves_failed_snapshot(
    settings, engine, client, db, owner, project, chapter
):
    old_id = pending_task(db, owner, project, kind="rewrite", chapter_id=chapter)

    async def failed(*args):
        raise ModelCallFailed()

    await execute_author(settings, engine, old_id, generate=failed)
    db.expire_all()
    old_snapshot = db.get(Task, old_id).input_snapshot
    client.put(
        f"/api/projects/{project['id']}", json={"constraints": "新保存的约束"}, headers=HEADERS
    )
    response = client.post(f"/api/tasks/{old_id}/retry", headers=HEADERS)
    assert response.status_code == 201
    new_id = UUID(response.json()["id"])
    seen = []

    async def generate(*args):
        seen.append(json.loads(args[-1][0]["content"]))
        return RESULT

    await execute_author(settings, engine, new_id, generate=generate)
    assert seen[0]["constraints"] == "新保存的约束"
    db.expire_all()
    assert db.get(Task, old_id).input_snapshot == old_snapshot
    assert db.get(Task, old_id).status == "failed"
    assert db.get(Task, new_id).retry_of == old_id


@pytest.mark.asyncio
async def test_account_disabled_during_author_call_finishes_failed_without_adoption(
    settings, engine, db, owner, project
):
    task_id = pending_task(db, owner, project)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(*args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return RESULT

    running = asyncio.create_task(execute_author(settings, engine, task_id, generate=generate))
    await asyncio.wait_for(started.wait(), 2)
    db.get(Account, owner).active = False
    db.commit()
    await asyncio.wait_for(running, 2)
    assert cancelled.is_set()
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.error == "account_unavailable"
    assert task.finished_at is not None and task.adopted_at is None
    assert db.scalar(select(func.count()).select_from(Chapter)) == 0


def test_slow_author_worker_does_not_occupy_chat_consumer(
    settings, engine, db, owner, project, monkeypatch
):
    from lisa import author, chat

    writing_id = pending_task(db, owner, project)
    chat_id = ingest_message(
        db,
        bot_id="920000001",
        message_id="1",
        qq_id="720000001",
        sent_at=datetime.now(UTC),
        content="作者生成期间仍然聊天。",
    )
    db.commit()
    chat_job_id = db.get(Task, chat_id).queue_job_id
    db.execute(
        text("UPDATE procrastinate_jobs SET scheduled_at=now() WHERE id=:id"), {"id": chat_job_id}
    )
    db.commit()

    async def exercise():
        started, release, replied = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_author(*args):
            started.set()
            await release.wait()
            return RESULT

        async def chat_reply(*args):
            replied.set()
            return Dialogue(reply="作者在工作，聊天仍可继续。", opinions=[], commission=None)

        monkeypatch.setattr(author, "generate_writing", slow_author)
        monkeypatch.setattr(chat, "generate_dialogue", chat_reply)
        queue = create_queue(settings)
        async with queue.open_async():
            author_worker = asyncio.create_task(
                queue.run_worker_async(
                    queues=["lisa_author"],
                    wait=False,
                    concurrency=1,
                    install_signal_handlers=False,
                )
            )
            try:
                # Database/queue startup on Windows is independent of model latency.
                await asyncio.wait_for(started.wait(), 10)
                await asyncio.wait_for(
                    queue.run_worker_async(
                        queues=["lisa"],
                        wait=False,
                        concurrency=1,
                        install_signal_handlers=False,
                    ),
                    5,
                )
                assert replied.is_set()
                with Session(engine) as session:
                    assert session.get(Task, writing_id).status == "running"
                    assert session.get(Task, chat_id).status == "completed"
            finally:
                release.set()
                await asyncio.wait_for(author_worker, 5)

    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(exercise())
    db.expire_all()
    assert db.get(Task, writing_id).status == "completed"
