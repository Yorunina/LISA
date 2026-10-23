from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from lisa import scheduling
from lisa.models import Account, Task
from lisa.queue import create_queue, enqueue_author


@pytest.fixture
def autonomy_settings(settings):
    return settings.model_copy(update={"autonomy_enabled": True, "embedding_model": ""})


@pytest.fixture
def owner(db):
    item = Account(username="schedule_queue_reader", password_hash="synthetic-unused")
    db.add(item)
    db.commit()
    return item.id


def test_autonomy_schedule_disabled_by_default(settings, monkeypatch):
    assert settings.autonomy_enabled is False
    enqueue = Mock(side_effect=AssertionError("Disabled schedule must not scan or queue"))
    engine = Mock(side_effect=AssertionError("Disabled schedule must not connect"))
    monkeypatch.setattr(scheduling, "enqueue_due_plans", enqueue)
    monkeypatch.setattr("lisa.db.make_engine", engine)
    queue = create_queue(settings)
    assert ("lisa.schedule_sweep", "") not in queue.periodic_registry.periodic_tasks
    queue.tasks["lisa.schedule_sweep"].func(timestamp=0)
    enqueue.assert_not_called()
    engine.assert_not_called()


def test_autonomy_sweep_uses_existing_author_consumer(autonomy_settings):
    queue = create_queue(autonomy_settings)
    entry = queue.periodic_registry.periodic_tasks[("lisa.schedule_sweep", "")]
    assert entry.cron == "* * * * *"
    assert entry.task.queue == "lisa_author" and entry.task.lock == "schedule-sweep"
    assert ("lisa.index_sweep", "") not in queue.periodic_registry.periodic_tasks


def test_sweep_commits_business_task_and_queue_in_one_transaction(
    autonomy_settings,
    engine,
    owner,
    monkeypatch,
):
    created = []

    def enqueue(db, settings):
        assert settings.autonomy_enabled
        task = Task(user_id=owner, kind="scene", source="autonomous", status="pending")
        db.add(task)
        db.flush()
        task.queue_job_id = enqueue_author(db, task.id, owner)
        created.append((task.id, task.queue_job_id))

    monkeypatch.setattr(scheduling, "enqueue_due_plans", enqueue)
    create_queue(autonomy_settings).tasks["lisa.schedule_sweep"].func(timestamp=0)
    assert len(created) == 1
    task_id, job_id = created[0]
    with Session(engine) as check:
        task = check.get(Task, task_id)
        assert task.queue_job_id == job_id and task.status == "pending"
        queued = check.execute(
            text("SELECT queue_name, task_name, args, lock FROM procrastinate_jobs WHERE id=:id"),
            {"id": job_id},
        ).one()
        assert queued.queue_name == "lisa_author" and queued.task_name == "lisa.author"
        assert queued.args == {"task_id": str(task_id)}
        assert queued.lock == f"author:{owner}"


def test_sweep_failure_rolls_back_queued_work(autonomy_settings, engine, owner, monkeypatch):
    created = []

    def fail(db, settings):
        task = Task(user_id=owner, kind="scene", source="autonomous", status="pending")
        db.add(task)
        db.flush()
        task.queue_job_id = enqueue_author(db, task.id, owner)
        created.append((task.id, task.queue_job_id))
        raise RuntimeError("synthetic schedule failure")

    monkeypatch.setattr(scheduling, "enqueue_due_plans", fail)
    with pytest.raises(RuntimeError, match="synthetic schedule failure"):
        create_queue(autonomy_settings).tasks["lisa.schedule_sweep"].func(timestamp=0)
    task_id, job_id = created[0]
    with Session(engine) as check:
        assert check.get(Task, task_id) is None
        assert (
            check.scalar(text("SELECT id FROM procrastinate_jobs WHERE id=:id"), {"id": job_id})
            is None
        )
