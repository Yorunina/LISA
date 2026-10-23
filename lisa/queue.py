import json
from datetime import UTC, datetime, timedelta

import procrastinate
from procrastinate.exceptions import JobRetry
from procrastinate.retry import BaseRetryStrategy
from sqlalchemy import text
from sqlalchemy.orm import Session

from lisa.retrieval import indexing_enabled
from lisa.settings import Settings


class ExplicitRetry(BaseRetryStrategy):
    def get_retry_decision(self, *, exception, job):
        # Preserve the task's requested delay; unknown and unexpected failures stay final.
        return exception.retry_decision if isinstance(exception, JobRetry) else None


def create_queue(settings: Settings | None = None) -> procrastinate.App:
    settings = settings or Settings()
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings.conninfo))
    # Registration is local to the factory, shared by API/worker and isolated tests.
    from lisa.chat import execute_chat

    @app.task(name="lisa.chat", queue="lisa", retry=ExplicitRetry())
    async def chat_task(task_id: str):
        from lisa.db import make_engine

        engine = make_engine(settings)
        try:
            await execute_chat(settings, engine, task_id)
        finally:
            engine.dispose()

    @app.task(name="lisa.author", queue="lisa_author", retry=ExplicitRetry())
    async def author_task(task_id: str):
        from lisa.author import execute_author
        from lisa.db import make_engine

        engine = make_engine(settings)
        try:
            await execute_author(settings, engine, task_id)
        finally:
            engine.dispose()

    @app.task(name="lisa.index", queue="lisa_index", retry=ExplicitRetry())
    async def index_task(task_id: str):
        from lisa.db import make_engine
        from lisa.retrieval import execute_index

        engine = make_engine(settings)
        try:
            await execute_index(settings, engine, task_id)
        finally:
            engine.dispose()

    @app.task(name="lisa.index_sweep", queue="lisa_index", lock="index-sweep")
    def index_sweep(timestamp: int):
        from lisa.db import make_engine

        if not indexing_enabled(settings):
            return
        engine = make_engine(settings)
        try:
            with Session(engine) as db, db.begin():
                enqueue_pending_indexes(db, settings)
        finally:
            engine.dispose()

    if indexing_enabled(settings):
        app.periodic(cron="* * * * *")(index_sweep)

    @app.task(name="lisa.schedule_sweep", queue="lisa_author", lock="schedule-sweep")
    def schedule_sweep(timestamp: int):
        if not settings.autonomy_enabled:
            return
        from lisa.db import make_engine
        from lisa.scheduling import enqueue_due_plans

        engine = make_engine(settings)
        try:
            with Session(engine) as db, db.begin():
                enqueue_due_plans(db, settings)
        finally:
            engine.dispose()

    if settings.autonomy_enabled:
        app.periodic(cron="* * * * *")(schedule_sweep)

    @app.task(name="lisa.contact", queue="lisa", retry=ExplicitRetry())
    async def contact_task(task_id: str):
        from lisa.db import make_engine
        from lisa.proactive import execute_contact

        engine = make_engine(settings)
        try:
            await execute_contact(settings, engine, task_id)
        finally:
            engine.dispose()

    @app.task(name="lisa.contact_sweep", queue="lisa", lock="contact-sweep")
    def contact_sweep(timestamp: int):
        if not settings.proactive_enabled:
            return
        from lisa.db import make_engine
        from lisa.proactive import enqueue_due_contacts

        engine = make_engine(settings)
        try:
            with Session(engine) as db, db.begin():
                enqueue_due_contacts(db, settings)
        finally:
            engine.dispose()

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
    return _enqueue(db, task_id, "lisa.chat", "lisa", f"chat:{user_id}", delay=2)


def enqueue_contact(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.contact", "lisa", f"chat:{user_id}")


def enqueue_author(db: Session, task_id, user_id) -> int:
    return _enqueue(db, task_id, "lisa.author", "lisa_author", f"author:{user_id}")


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
