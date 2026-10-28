import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from lisa import dialogue_skill
from lisa.chat_style import expression_guide
from lisa.model_calls import ModelCallFailed
from lisa.models import Account, ChatMessage, Memory, ModelCall, OutboundMessage, Project, Task
from lisa.recovery import recover_tasks
from lisa.style_distillation import TrialOutput, cleanup_trials, execute_style

HEADERS = {"X-LISA-Request": "1"}
PASSWORD = "synthetic-password-2026"


@pytest.fixture
def trial_owner(client, invites):
    response = client.post(
        "/api/auth/register",
        json={"username": "single_trial_reader", "password": PASSWORD,
              "invite_code": invites[0]},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def start(client, **changes):
    return client.post(
        "/api/style/trials",
        json={"kind": "personality", "prompt": "表达直接，适合时简短接话。",
              "input": "今天终于弄完了。", **changes},
        headers=HEADERS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "core", "examples", "corpus"])
async def test_single_pass_trial_freezes_shared_guide_and_never_adopts_side_effects(
    client, trial_owner, settings, engine, db, monkeypatch, mode
):
    monkeypatch.setattr(
        dialogue_skill, "compile_dialogue_skill", lambda selected: f"共有-{selected}"
    )
    response = start(client, dialogue_skill_mode=mode)
    assert response.status_code == 201, response.text
    assert response.json()["planned_calls"] == 1
    task_id = UUID(response.json()["id"])
    task = db.get(Task, task_id)
    guide = task.request["dialogue_guide"]
    expected = expression_guide() if mode == "off" else f"共有-{mode}"
    assert guide == {"mode": mode, "text": expected,
                     "sha256": hashlib.sha256(expected.encode()).hexdigest()}
    # A file/config change after enqueue must not change an existing trial.
    monkeypatch.setattr(dialogue_skill, "compile_dialogue_skill", lambda selected: "已变化的指引")
    monkeypatch.setattr(settings, "dialogue_skill_mode", "review")
    seen = []

    async def generate(*args, **kwargs):
        seen.append((args, kwargs))
        return TrialOutput(text="总算弄完了。")

    await execute_style(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "completed" and len(seen) == 1
    assert task.request == {"kind": "personality"}
    assert expected in seen[0][0][-2] and "已变化的指引" not in seen[0][0][-2]
    assert "表达直接，适合时简短接话。" in seen[0][0][-2]
    assert seen[0][1]["schema"] is TrialOutput
    assert task.result == {"text": "总算弄完了。", "isolated": True,
                           "dialogue_skill": {"mode": mode, "sha256": guide["sha256"]}}
    assert all(db.scalar(select(func.count()).select_from(model)) == 0
               for model in (ChatMessage, OutboundMessage, Memory, Project))


def test_default_follows_single_pass_setting_and_review_is_explicitly_rejected(
    client, trial_owner, settings, db, monkeypatch
):
    monkeypatch.setattr(settings, "dialogue_skill_mode", "off")
    response = start(client)
    assert response.status_code == 201
    first = db.get(Task, UUID(response.json()["id"]))
    assert first.request["dialogue_guide"]["mode"] == "off"
    client.post(f"/api/style/tasks/{first.id}/cancel", headers=HEADERS)
    monkeypatch.setattr(settings, "dialogue_skill_mode", "core")
    response = start(client)
    assert response.status_code == 201
    second = db.get(Task, UUID(response.json()["id"]))
    assert second.request["dialogue_guide"]["mode"] == "core"
    client.post(f"/api/style/tasks/{second.id}/cancel", headers=HEADERS)
    monkeypatch.setattr(settings, "dialogue_skill_mode", "review")
    response = start(client)
    assert response.status_code == 422 and "单次生成" in response.json()["detail"]
    assert start(client, dialogue_skill_mode="review").status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 2
    assert start(client, dialogue_skill_mode="core").status_code == 201


@pytest.mark.parametrize("changes", [
    {"dialogue_guide": {"mode": "core", "text": "客户端伪造指引"}},
    {"kind": "writing", "dialogue_skill_mode": "core"},
    {"mode": "refine", "wrong": "旧回复", "correction": "改正", "dialogue_skill_mode": "off"},
])
def test_internal_snapshot_and_inapplicable_modes_are_not_accepted(client, trial_owner, changes):
    assert start(client, **changes).status_code == 422


def test_missing_private_resources_fail_without_queueing_or_exposing_local_path(
    client, trial_owner, db, monkeypatch
):
    def missing(mode):
        raise FileNotFoundError("private/deployment/source.txt")

    monkeypatch.setattr(dialogue_skill, "compile_dialogue_skill", missing)
    response = start(client, dialogue_skill_mode="corpus")
    assert response.status_code == 422
    assert "source.txt" not in response.text and "部署资源" in response.text
    assert db.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "hash", "review"])
async def test_new_trial_with_damaged_snapshot_fails_closed_without_generation(
    client, trial_owner, settings, engine, db, damage
):
    response = start(client, dialogue_skill_mode="core")
    task_id = UUID(response.json()["id"])
    task = db.get(Task, task_id)
    request = dict(task.request)
    if damage == "missing":
        request.pop("dialogue_guide")
    else:
        request["dialogue_guide"] = {**request["dialogue_guide"],
                                     ("sha256" if damage == "hash" else "mode"): damage}
    task.request = request
    db.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("damaged snapshots must not trigger a model call")

    await execute_style(settings, engine, task_id, generate=forbidden)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == "failed" and task.result is None
    assert task.request == {"kind": "personality"}


@pytest.mark.asyncio
async def test_legacy_queued_trial_uses_legacy_guide_without_loading_new_resources(
    trial_owner, settings, engine, db, monkeypatch
):
    task = Task(user_id=trial_owner, kind="style_trial", source="web", status="pending",
                request={"kind": "personality", "prompt": "旧的风格卡", "input": "旧来话"})
    db.add(task)
    db.commit()
    monkeypatch.setattr(settings, "dialogue_skill_mode", "review")

    def forbidden(*args, **kwargs):
        pytest.fail("legacy trials must not resolve current skill resources")

    monkeypatch.setattr(dialogue_skill, "compile_dialogue_skill", forbidden)
    seen = []

    async def generate(*args, **kwargs):
        seen.append(args[-2])
        return TrialOutput(text="旧试聊结果")

    await execute_style(settings, engine, task.id, generate=generate)
    db.expire_all()
    assert expression_guide() in seen[0]
    assert db.get(Task, task.id).result == {"text": "旧试聊结果", "isolated": True}


@pytest.mark.parametrize("finish", ["cancel", "expire", "delete"])
def test_pending_trial_snapshot_is_erased_on_terminal_cleanup(
    client, trial_owner, db, finish
):
    response = start(client, dialogue_skill_mode="core")
    assert response.status_code == 201
    task_id = UUID(response.json()["id"])
    assert db.get(Task, task_id).request["dialogue_guide"]["text"]
    if finish == "cancel":
        assert client.post(f"/api/style/tasks/{task_id}/cancel", headers=HEADERS).status_code == 200
    elif finish == "expire":
        db.get(Task, task_id).created_at = datetime.now(UTC) - timedelta(days=2)
        db.commit()
        assert cleanup_trials(db) == 1
        db.commit()
    else:
        response = client.request("DELETE", "/api/auth/account", json={"password": PASSWORD},
                                  headers=HEADERS)
        assert response.status_code == 204
    db.expire_all()
    task = db.get(Task, task_id)
    assert task is None if finish == "delete" else task.request == {"kind": "personality"}


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["failure", "cancel", "deactivate"])
async def test_inflight_terminal_state_erases_snapshot_and_rejects_late_result(
    client, trial_owner, settings, engine, db, finish
):
    response = start(client, dialogue_skill_mode="core")
    task_id = UUID(response.json()["id"])

    async def generate(*args, **kwargs):
        if finish == "failure":
            raise ModelCallFailed("synthetic failure")
        if finish == "cancel":
            client.post(f"/api/style/tasks/{task_id}/cancel", headers=HEADERS)
        else:
            with Session(engine) as session, session.begin():
                session.get(Account, trial_owner).active = False
        await asyncio.sleep(0)
        return TrialOutput(text="不能采用的迟到结果")

    await execute_style(settings, engine, task_id, generate=generate)
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status in {"failed", "cancelled"} and task.result is None
    assert task.request == {"kind": "personality"}


@pytest.mark.parametrize("queue_status", ["doing", "failed"])
def test_recovery_preserves_only_resumable_trial_snapshot(
    client, trial_owner, engine, db, queue_status
):
    response = start(client, dialogue_skill_mode="core")
    assert response.status_code == 201
    task_id = UUID(response.json()["id"])
    task = db.get(Task, task_id)
    original = dict(task.request)
    job_id = task.queue_job_id
    task.status, task.started_at = "running", datetime.now(UTC)
    db.execute(
        text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": job_id}
    )
    if queue_status == "failed":
        db.execute(
            text("SELECT procrastinate_finish_job_v1(:id, 'failed', false)"), {"id": job_id}
        )
    db.commit()

    assert recover_tasks(engine) == 1
    db.expire_all()
    task = db.get(Task, task_id)
    queue_row = db.execute(
        text("SELECT status, attempts FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}
    ).one()
    if queue_status == "doing":
        assert task.status == "pending" and task.request == original
        assert queue_row.status == "todo" and queue_row.attempts == 1
    else:
        assert task.status == "failed" and task.error == "worker_interrupted"
        assert task.request == {"kind": "personality"}
        assert queue_row.status == "failed"
    assert task.result is None
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
    assert recover_tasks(engine) == 0
