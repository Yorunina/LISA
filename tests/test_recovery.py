import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from lisa import model_calls
from lisa.chat import ingest_message
from lisa.models import (
    Account,
    BudgetDay,
    Chapter,
    ChapterVersion,
    ModelCall,
    OutboundMessage,
    Persona,
    Project,
    QQBinding,
    StyleCorpus,
    Task,
)
from lisa.queue import _enqueue, create_queue
from lisa.recovery import recover_tasks, task_execution
from lisa.works import create_writing_task, task_view

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def owner(db):
    # Other tests may have left queued rows whose business data has been cleared.
    db.execute(text("DELETE FROM procrastinate_jobs"))
    db.execute(text("DELETE FROM procrastinate_workers"))
    user = Account(username="recovery_reader", password_hash="unused-synthetic")
    db.add(user)
    db.flush()
    db.add_all([
        QQBinding(user_id=user.id, qq_id="750000001"),
        Persona(user_id=user.id, name="Synthetic", prompt="Synthetic persona.", version=1),
    ])
    db.commit()
    return user.id


def queued(db, owner, *, kind="scene", status="running"):
    task = Task(user_id=owner, kind=kind, status=status, started_at=datetime.now(UTC))
    db.add(task)
    db.flush()
    name, queue = {
        "scene": ("author", "lisa_author"),
        "chat": ("chat", "lisa"),
        "index": ("index", "lisa_index"),
        "contact": ("contact", "lisa"),
        "style_distill": ("style", "lisa_style"),
    }[kind]
    task.queue_job_id = _enqueue(db, task.id, f"lisa.{name}", queue, f"{name}:{owner}")
    db.execute(
        text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"),
        {"id": task.queue_job_id},
    )
    db.commit()
    return task


def reserve(settings, engine, task):
    return model_calls._reserve(settings, engine, task.id, task.user_id, Decimal("0.01"))


def test_recovery_settles_unknown_once_and_releases_global_slot(settings, engine, db, owner):
    task = queued(db, owner)
    call_id = reserve(settings, engine, task)
    assert recover_tasks(engine) == 1
    assert recover_tasks(engine) == 0
    # A late local settlement cannot refund/change an unknown recovered outcome.
    model_calls._finish(engine, call_id, state="succeeded", charged=Decimal("0.001"))
    db.expire_all()
    call, day = db.get(ModelCall, call_id), db.scalar(select(BudgetDay))
    assert call.state == "unknown" and call.charged_usd == Decimal("0.01")
    assert day.reserved_usd == 0 and day.spent_usd == Decimal("0.01")
    assert task.status == "failed" and task.error == "model_unknown"
    assert not task_view(db, task).can_retry
    new_task = queued(db, owner)
    assert reserve(settings, engine, new_task)


def test_recovery_does_not_interrupt_a_live_execution_lease(settings, engine, db, owner):
    task = queued(db, owner)
    call_id = reserve(settings, engine, task)
    # Even a missing/stale queue heartbeat cannot supersede a live execution.
    with task_execution(engine, task.id):
        assert recover_tasks(engine) == 0
    db.expire_all()
    assert task.status == "running" and db.get(ModelCall, call_id).state == "reserved"
    assert recover_tasks(engine) == 1


def test_recovery_preserves_cancelled_and_cleans_distillation_source(settings, engine, db, owner):
    task = queued(db, owner, kind="style_distill")
    reserve(settings, engine, task)
    corpus = StyleCorpus(
        user_id=owner, kind="chat", task_id=task.id, state="processing",
        payload={"text": "private synthetic source"}, preview={"participants": ["speaker"]},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(corpus)
    task.status, task.error = "cancelled", "user_cancelled"
    db.commit()
    assert recover_tasks(engine) == 1
    db.expire_all()
    assert task.status == "cancelled" and task.error == "user_cancelled"
    assert corpus.payload == {} and corpus.state == "cleaned" and corpus.cleaned_at
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_recovery_retries_only_without_provider_attempts_and_keeps_scope(engine, db, owner):
    task = queued(db, owner)
    task.input_snapshot = {"scope": "original finite scene"}
    original_start, job_id = task.started_at, task.queue_job_id
    db.commit()
    assert recover_tasks(engine) == 1
    db.expire_all()
    assert task.status == "pending" and task.started_at == original_start
    assert task.input_snapshot == {"scope": "original finite scene"}
    row = db.execute(
        text("SELECT id, status, attempts FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}
    ).one()
    assert row.id == job_id and row.status == "todo" and row.attempts == 1
    assert recover_tasks(engine) == 0


@pytest.mark.parametrize("queue_status", ["failed", "succeeded"])
def test_recovery_reconciles_unhandled_queue_failures(engine, db, owner, queue_status):
    task = queued(db, owner)
    db.execute(
        text("SELECT procrastinate_finish_job_v1(:id, :state, false)"),
        {"id": task.queue_job_id, "state": queue_status},
    )
    db.commit()
    assert recover_tasks(engine) == 1
    db.expire_all()
    assert task.status == "failed" and task.error == "worker_interrupted"
    assert task_view(db, task).can_retry


def child(mode, **env):
    return subprocess.Popen(
        [sys.executable, str(ROOT / "tests/recovery_child.py"), mode],
        cwd=ROOT, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def wait_for_state(engine, task_id, mode, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with Session(engine) as check:
            task = check.get(Task, task_id)
            calls = list(check.scalars(select(ModelCall).where(ModelCall.task_id == task_id)))
            ready = {
                "before_call": task.status == "running" and not calls,
                "inflight": any(call.state == "reserved" for call in calls),
                "settled": any(call.state == "succeeded" for call in calls),
                "adopted": task.adopted_at is not None,
            }[mode]
            if ready:
                return
        if process.poll() is not None:
            pytest.fail(process.communicate()[0])
        time.sleep(0.05)
    pytest.fail("Synthetic worker did not reach the interruption boundary")


@pytest.mark.parametrize("mode", ["before_call", "inflight", "settled", "adopted", "cancelled"])
def test_recovery_real_worker_kill_and_restart(settings, engine, db, owner, mode):
    work = Project(user_id=owner, title="Synthetic", agreement="One short scene.")
    db.add(work)
    db.flush()
    task = create_writing_task(
        db, db.get(Account, owner), work, "scene", "One short scene.", source="web"
    )
    task_id, work_id = task.id, work.id
    db.commit()
    running = child("inflight" if mode == "cancelled" else mode)
    try:
        wait_for_state(engine, task_id, "inflight" if mode == "cancelled" else mode, running)
        # Persist the API's cancellation state before the process is killed;
        # the HTTP cancellation path itself is covered by test_author.py.
        if mode == "cancelled":
            db.expire_all()
            task.status, task.error = "cancelled", "user_cancelled"
            db.commit()
    finally:
        running.kill()
        running.communicate(timeout=10)
    # Advance only the dead test worker's heartbeat; avoid waiting a real minute.
    db.execute(text("UPDATE procrastinate_workers SET last_heartbeat=now()-interval '2 minutes'"))
    db.commit()
    restarted = child("complete")
    try:
        output = restarted.communicate(timeout=30)[0]
        assert restarted.returncode == 0, output
    finally:
        if restarted.poll() is None:
            restarted.kill()
            restarted.communicate(timeout=10)
    db.expire_all()
    assert task.status == {
        "before_call": "completed", "inflight": "failed", "settled": "failed",
        "adopted": "completed", "cancelled": "cancelled",
    }[mode]
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 1
    chapters = db.scalar(
        select(func.count()).select_from(Chapter).where(Chapter.project_id == work_id)
    )
    assert chapters == (
        1 if mode in {"before_call", "adopted"} else 0
    )
    assert db.scalar(select(func.count()).select_from(ChapterVersion)) == (
        1 if mode in {"before_call", "adopted"} else 0
    )
    assert db.scalar(select(BudgetDay.reserved_usd)) == 0
    assert recover_tasks(engine) == 0
    # A new real chat job still completes after the interrupted author released its slot.
    next_id = ingest_message(
        db, bot_id="950000001", message_id="next", qq_id="750000001",
        sent_at=datetime.now(UTC), content="Synthetic next conversation.",
    )
    # --once intentionally skips the future two-second message-merging delay.
    db.execute(
        text("UPDATE procrastinate_jobs SET scheduled_at=now() WHERE id=:id"),
        {"id": db.get(Task, next_id).queue_job_id},
    )
    db.commit()
    subsequent = child("complete")
    try:
        output = subsequent.communicate(timeout=30)[0]
        assert subsequent.returncode == 0, output
    finally:
        if subsequent.poll() is None:
            subsequent.kill()
            subsequent.communicate(timeout=10)
    db.expire_all()
    assert db.get(Task, next_id).status == "completed"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 2


def test_recovery_registered_independent_of_autonomy(settings):
    queue = create_queue(settings)
    assert queue.periodic_registry.periodic_tasks[("lisa.recovery_sweep", "")].cron == "* * * * *"


def test_recovery_releases_dead_periodic_lock_but_preserves_live_scan(engine, db, owner):
    job_ids = []
    for timestamp, name, queue, lock in (
        (1, "lisa.style_cleanup", "lisa_style", "style-cleanup"),
        (2, "lisa.index_sweep", "lisa_index", "index-sweep"),
    ):
        job_id = db.scalar(text("""
            SELECT unnest(procrastinate_defer_jobs_v1(ARRAY[ROW(
                :queue, :name, 0, :lock, NULL,
                CAST(:args AS jsonb), now()
            )::procrastinate_job_to_defer_v1]))
        """), {"args": json.dumps({"timestamp": timestamp}),
                "name": name, "queue": queue, "lock": lock})
        job_ids.append(job_id)
        db.execute(
            text("UPDATE procrastinate_jobs SET status='doing' WHERE id=:id"), {"id": job_id}
        )
    db.commit()
    with task_execution(engine, "lisa.index_sweep:2"):
        assert recover_tasks(engine) == 1
        rows = db.execute(text(
            "SELECT status FROM procrastinate_jobs WHERE id IN (:first, :second) ORDER BY id"
        ), {"first": job_ids[0], "second": job_ids[1]}).scalars().all()
        assert rows == ["failed", "doing"]
        db.commit()
    assert recover_tasks(engine) == 1


def test_recovery_defers_to_a_current_worker_heartbeat(settings, engine, db, owner):
    task = queued(db, owner)
    call_id = reserve(settings, engine, task)
    worker_id = db.scalar(text("SELECT procrastinate_register_worker_v1()"))
    db.execute(text("UPDATE procrastinate_jobs SET worker_id=:worker WHERE id=:id"), {
        "worker": worker_id, "id": task.queue_job_id,
    })
    db.commit()
    assert recover_tasks(engine) == 0
    db.expire_all()
    assert db.get(ModelCall, call_id).state == "reserved"


@pytest.mark.asyncio
async def test_recovery_real_gateway_kill_keeps_unknown_and_sends_pending(engine, db, owner):
    sent, first_request, second_request = [], asyncio.Event(), asyncio.Event()
    for position in range(2):
        task = Task(user_id=owner, kind="chat", status="completed")
        db.add(task)
        db.flush()
        db.add(OutboundMessage(
            user_id=owner, task_id=task.id, qq_id="750000001", content=f"synthetic-{position}",
            created_at=datetime.now(UTC) + timedelta(seconds=position),
        ))
    db.commit()

    async def server(socket):
        try:
            async for raw in socket:
                request = json.loads(raw)
                if request["action"] == "get_login_info":
                    data = {"user_id": 950000001}
                elif request["action"] == "get_version_info":
                    data = {"app_name": "synthetic", "app_version": "1"}
                else:
                    sent.append(request["params"]["message"][0]["data"]["text"])
                    if sent[-1] == "synthetic-0":
                        first_request.set()
                        continue  # Simulate delivery whose acknowledgement never arrives.
                    data = {"message_id": 1234}
                await socket.send(json.dumps({
                    "echo": request["echo"], "status": "ok", "retcode": 0, "data": data,
                }))
                if request["action"] == "send_private_msg":
                    second_request.set()
        except ConnectionClosed:
            pass

    async with serve(server, "127.0.0.1", 0) as listener:
        env = {
            "LISA_ONEBOT_WS_URL": f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}",
            "LISA_ONEBOT_TOKEN": "synthetic-token", "LISA_CHAT_ENABLED": "true",
        }
        running = child("gateway", **env)
        try:
            await asyncio.wait_for(first_request.wait(), 15)
        finally:
            running.kill()
            await asyncio.to_thread(running.communicate, timeout=10)
        db.expire_all()
        assert db.scalar(select(OutboundMessage.status).where(
            OutboundMessage.content == "synthetic-0"
        )) == "sending"
        restarted = child("gateway", **env)
        try:
            await asyncio.wait_for(second_request.wait(), 15)
            for _ in range(100):
                with Session(engine) as check:
                    status = check.scalar(select(OutboundMessage.status).where(
                        OutboundMessage.content == "synthetic-1"
                    ))
                if status == "sent":
                    break
                await asyncio.sleep(0.02)
            assert status == "sent"
        finally:
            restarted.kill()
            await asyncio.to_thread(restarted.communicate, timeout=10)
    db.expire_all()
    first = db.scalar(select(OutboundMessage).where(OutboundMessage.content == "synthetic-0"))
    assert first.status == "unknown" and first.attempts == 1
    assert sent == ["synthetic-0", "synthetic-1"]
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
