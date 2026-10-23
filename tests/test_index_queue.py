import asyncio
import os
from decimal import Decimal
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lisa import retrieval
from lisa.models import Account, Task
from lisa.queue import create_queue, enqueue_index, enqueue_pending_indexes


@pytest.fixture
def enabled_settings(settings):
    return settings.model_copy(
        update={
            "embedding_model": "synthetic-index-model",
            "embedding_input_per_million": Decimal("1"),
            "model_api_key": SecretStr("synthetic-key"),
        }
    )


@pytest.fixture
def owner(db):
    user = Account(username="index_queue_test", password_hash="synthetic-unused")
    db.add(user)
    db.commit()
    return user.id


def test_index_task_and_queue_row_commit_together(enabled_settings, engine, db, owner, monkeypatch):
    def ensure(session, settings, *, limit):
        assert limit == 20
        task = Task(user_id=owner, kind="index", status="pending")
        session.add(task)
        session.flush()
        return [task]

    monkeypatch.setattr(retrieval, "ensure_index_tasks", ensure)
    assert enqueue_pending_indexes(db, enabled_settings) == 1
    task = db.scalar(select(Task).where(Task.user_id == owner))
    task_id, job_id = task.id, task.queue_job_id
    db.commit()
    with Session(engine) as check:
        assert check.get(Task, task_id).queue_job_id == job_id
        row = check.execute(
            text("SELECT queue_name, task_name, args, lock FROM procrastinate_jobs WHERE id=:id"),
            {"id": job_id},
        ).one()
        assert row.queue_name == "lisa_index" and row.task_name == "lisa.index"
        assert row.args == {"task_id": str(task_id)} and row.lock == f"index:{owner}"


def test_index_queue_rolls_back_with_task(enabled_settings, engine, db, owner, monkeypatch):
    def ensure(session, settings, *, limit):
        task = Task(user_id=owner, kind="index", status="pending")
        session.add(task)
        session.flush()
        return [task]

    monkeypatch.setattr(retrieval, "ensure_index_tasks", ensure)
    enqueue_pending_indexes(db, enabled_settings)
    task = db.scalar(select(Task).where(Task.user_id == owner))
    task_id, job_id = task.id, task.queue_job_id
    db.rollback()
    with Session(engine) as check:
        assert check.get(Task, task_id) is None
        assert (
            check.scalar(text("SELECT id FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
            is None
        )


@pytest.mark.parametrize(
    "change",
    [
        {"embedding_model": ""},
        {"embedding_input_per_million": Decimal(0)},
        {"model_api_key": SecretStr("")},
    ],
)
def test_disabled_index_sweep_has_no_database_or_provider_work(
    enabled_settings, monkeypatch, change
):
    settings = enabled_settings.model_copy(update=change)
    ensure = Mock(side_effect=AssertionError("Disabled indexing must not scan"))
    monkeypatch.setattr(retrieval, "ensure_index_tasks", ensure)
    db = Mock()
    assert enqueue_pending_indexes(db, settings) == 0
    queue = create_queue(settings)
    assert ("lisa.index_sweep", "") not in queue.periodic_registry.periodic_tasks
    queue.tasks["lisa.index_sweep"].func(timestamp=0)
    ensure.assert_not_called()
    db.flush.assert_not_called()


def test_minute_sweep_registered_and_real_failures_not_retried(enabled_settings):
    queue = create_queue(enabled_settings)
    periodic = queue.periodic_registry.periodic_tasks[("lisa.index_sweep", "")]
    assert periodic.cron == "* * * * *" and periodic.task.queue == "lisa_index"
    assert periodic.task.lock == "index-sweep"
    index = queue.tasks["lisa.index"]
    assert index.queue == "lisa_index"
    assert (
        index.retry_strategy.get_retry_decision(exception=RuntimeError("failed"), job=None) is None
    )


def test_persisted_index_dispatches_registered_consumer(settings, engine, db, owner, monkeypatch):
    seen = []

    async def execute(settings, engine, task_id):
        seen.append(task_id)

    monkeypatch.setattr(retrieval, "execute_index", execute)
    task = Task(user_id=owner, kind="index", status="pending")
    db.add(task)
    db.flush()
    task_id = task.id
    task.queue_job_id = enqueue_index(db, task.id, owner)
    job_id = task.queue_job_id
    # Isolate this consumer run from other tests' durable pending jobs.
    db.execute(
        text("UPDATE procrastinate_jobs SET queue_name='lisa_index_test' WHERE id=:id"),
        {"id": job_id},
    )
    db.commit()
    queue = create_queue(settings)

    async def exercise():
        async with queue.open_async():
            await asyncio.wait_for(
                queue.run_worker_async(
                    queues=["lisa_index_test"],
                    wait=False,
                    concurrency=1,
                    install_signal_handlers=False,
                ),
                timeout=20,
            )

    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(exercise())
    assert seen == [str(task_id)]
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}
            )
            == "succeeded"
        )


def test_index_cli_only_queues(enabled_settings, monkeypatch, capsys):
    from lisa import __main__ as cli

    enqueue = Mock(return_value=2)
    monkeypatch.setattr(cli, "Settings", lambda: enabled_settings)
    monkeypatch.setattr(cli, "enqueue_pending_indexes", enqueue)
    monkeypatch.setattr("sys.argv", ["lisa", "index"])
    cli.main()
    enqueue.assert_called_once()
    assert "Queued 2 indexing tasks" in capsys.readouterr().out
