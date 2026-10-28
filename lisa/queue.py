import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID

import procrastinate
from procrastinate.exceptions import JobRetry
from procrastinate.retry import BaseRetryStrategy
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lisa.retrieval import indexing_enabled
from lisa.settings import Settings

CHAT_SILENCE_SECONDS = 10
logger = logging.getLogger(__name__)


class ExplicitRetry(BaseRetryStrategy):
    def get_retry_decision(self, *, exception, job):
        # Preserve the task's requested delay; unknown and unexpected failures stay final.
        return exception.retry_decision if isinstance(exception, JobRetry) else None


@contextmanager
def _execution_engine(settings, key):
    from lisa.db import make_engine
    from lisa.recovery import task_execution

    engine = make_engine(settings)
    try:
        with task_execution(engine, key):
            yield engine
    finally:
        engine.dispose()


async def _execute_task(settings, task_id, execute):
    with _execution_engine(settings, task_id) as engine:
        started = perf_counter()
        _log_task_wait(engine, task_id)
        outcome = "returned"
        try:
            await execute(settings, engine, task_id)
        except BaseException as exc:
            # No exception text: provider/SQL errors can contain private values.
            outcome = type(exc).__name__
            raise
        finally:
            logger.info(
                "task=%s execution_ms=%.1f outcome=%s",
                task_id, (perf_counter() - started) * 1000, outcome,
            )


def _log_task_wait(engine, task_id):
    from lisa.models import Task

    try:
        with Session(engine) as db:
            row = db.execute(
                select(Task.kind, Task.created_at).where(Task.id == UUID(str(task_id)))
            ).one_or_none()
            if row:
                logger.info(
                    "task=%s kind=%s task_age_ms=%.1f",
                    task_id, row.kind,
                    max(0, (datetime.now(UTC) - row.created_at).total_seconds() * 1000),
                )
    except (SQLAlchemyError, ValueError):
        # Observability must not change task execution or expose SQL parameters.
        logger.warning("task=%s queue_timing_unavailable", task_id)


def create_queue(settings: Settings | None = None) -> procrastinate.App:
    settings = settings or Settings()
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings.conninfo))
    # Registration is local to the factory, shared by API/worker and isolated tests.
    from lisa.chat import execute_chat

    @app.task(name="lisa.shared", queue="lisa_index", retry=ExplicitRetry())
    async def shared_task(task_id: str):
        from lisa.shared_jobs import execute_shared
        await _execute_task(settings, task_id, execute_shared)

    @app.task(name="lisa.chat", queue="lisa", retry=ExplicitRetry())
    async def chat_task(task_id: str):
        await _execute_task(settings, task_id, execute_chat)

    @app.task(name="lisa.image", queue="lisa_image", retry=ExplicitRetry())
    async def image_task(task_id: str):
        from lisa.images import execute_image

        await _execute_task(settings, task_id, execute_image)

    @app.task(name="lisa.author", queue="lisa_author", retry=ExplicitRetry())
    async def author_task(task_id: str):
        from lisa.author import execute_author

        await _execute_task(settings, task_id, execute_author)

    @app.task(name="lisa.index", queue="lisa_index", retry=ExplicitRetry())
    async def index_task(task_id: str):
        from lisa.retrieval import execute_index

        await _execute_task(settings, task_id, execute_index)

    @app.task(name="lisa.style", queue="lisa_style", retry=ExplicitRetry())
    async def style_task(task_id: str):
        from lisa.style_distillation import execute_style

        await _execute_task(settings, task_id, execute_style)

    @app.task(name="lisa.recovery_sweep", queue="lisa")
    def recovery_sweep(timestamp: int):
        from lisa.life_plans import cleanup_expired_plans
        from lisa.recovery import recover_tasks

        # No static queue lock: a dead sweep cannot block its own successor.
        with _execution_engine(settings, f"lisa.recovery_sweep:{timestamp}") as engine:
            recover_tasks(engine)
            cleanup_expired_plans(engine)
            from lisa.chat_context import cleanup_context
            from lisa.images import cleanup_images
            with Session(engine) as db, db.begin():
                cleanup_images(db)
                cleanup_context(db)

    # Recovery is operational cleanup, independent of autonomous/model features.
    app.periodic(cron="* * * * *")(recovery_sweep)

    @app.task(name="lisa.style_cleanup", queue="lisa_style", lock="style-cleanup")
    def style_cleanup(timestamp: int):
        from lisa.group_records import purge_expired_messages
        from lisa.style_corpus import clear_expired_corpora
        from lisa.style_distillation import cleanup_trials

        with _execution_engine(settings, f"lisa.style_cleanup:{timestamp}") as engine:
            # Group management serializes per group before locking affected accounts;
            # do not carry ordinary corpus-cleanup account locks into that operation.
            with Session(engine) as db, db.begin():
                purge_expired_messages(db)
            with Session(engine) as db, db.begin():
                clear_expired_corpora(db)
                cleanup_trials(db)

    # Cleanup protects temporary uploads even when autonomous features are disabled.
    app.periodic(cron="* * * * *")(style_cleanup)

    @app.task(name="lisa.index_sweep", queue="lisa_index", lock="index-sweep")
    def index_sweep(timestamp: int):
        if not indexing_enabled(settings):
            return
        with _execution_engine(settings, f"lisa.index_sweep:{timestamp}") as engine:
            with Session(engine) as db, db.begin():
                enqueue_pending_indexes(db, settings)

    if indexing_enabled(settings):
        app.periodic(cron="* * * * *")(index_sweep)

    @app.task(name="lisa.schedule_sweep", queue="lisa_author", lock="schedule-sweep")
    def schedule_sweep(timestamp: int):
        if not settings.autonomy_enabled:
            return
        from lisa.scheduling import enqueue_due_plans

        with _execution_engine(settings, f"lisa.schedule_sweep:{timestamp}") as engine:
            with Session(engine) as db, db.begin():
                enqueue_due_plans(db, settings)

    if settings.autonomy_enabled:
        app.periodic(cron="* * * * *")(schedule_sweep)

    @app.task(name="lisa.contact", queue="lisa", retry=ExplicitRetry())
    async def contact_task(task_id: str):
        from lisa.proactive import execute_contact

        await _execute_task(settings, task_id, execute_contact)

    @app.task(name="lisa.life_plan", queue="lisa", retry=ExplicitRetry())
    async def life_plan_task(task_id: str):
        from lisa.life_plans import execute_life_plan

        await _execute_task(settings, task_id, execute_life_plan)

    @app.task(name="lisa.contact_sweep", queue="lisa", lock="contact-sweep")
    def contact_sweep(timestamp: int):
        if not settings.proactive_enabled:
            return
        from lisa.proactive import enqueue_due_contacts

        with _execution_engine(settings, f"lisa.contact_sweep:{timestamp}") as engine:
            with Session(engine) as db, db.begin():
                enqueue_due_contacts(db, settings)

    if settings.proactive_enabled:
        app.periodic(cron="* * * * *")(contact_sweep)

    return app


def enqueue_pending_indexes(db: Session, settings: Settings, *, limit: int = 20) -> int:
    if not indexing_enabled(settings):
        return 0
    from lisa.retrieval import ensure_index_tasks

    tasks = ensure_index_tasks(db, settings, limit=limit)
    for task in tasks:
        task.queue_job_id = enqueue_index(db, task.id, task.user_id)
    db.flush()
    return len(tasks)


def enqueue_index(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.index", "lisa_index", f"index:{user_id}")


def enqueue_chat(db: Session, task_id, user_id) -> int:
    return _enqueue(
        db, task_id, "lisa.chat", "lisa", f"chat:{user_id}", delay=CHAT_SILENCE_SECONDS
    )


def reschedule_chat(db: Session, job_id: int, *, until: datetime) -> None:
    """Move a still-queued chat job to the latest persisted silence deadline."""
    db.execute(
        text(
            "UPDATE procrastinate_jobs SET scheduled_at=:until "
            "WHERE id=:id AND status='todo'"
        ),
        {"id": job_id, "until": until},
    )


def enqueue_contact(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.contact", "lisa", f"chat:{user_id}")


def enqueue_life_plan(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.life_plan", "lisa", f"life-plan:{user_id}")


def enqueue_author(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.author", "lisa_author", f"author:{user_id}")


def enqueue_style(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.style", "lisa_style", f"style:{user_id}")


def enqueue_image(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.image", "lisa_image", f"image:{user_id}")


def _enqueue(db, task_id, task_name, queue_name, lock, *, delay=0) -> int:
    """Use Procrastinate's versioned SQL API in the business transaction."""
    job_ids = db.scalar(
        text("""
        SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(
            :queue, :task, 0, :lock, NULL, CAST(:args AS jsonb), :scheduled_at
        )::procrastinate_job_to_defer_v1])
    """),
        {
            "queue": queue_name,
            "task": task_name,
            "lock": lock,
            "args": json.dumps({"task_id": str(task_id)}),
            "scheduled_at": datetime.now(UTC) + timedelta(seconds=delay),
        },
    )
    return job_ids[0]
