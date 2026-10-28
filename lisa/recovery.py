"""Reconcile abandoned Procrastinate jobs with their business tasks and call ledger."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from lisa.models import Account, BudgetDay, ChatMessage, ModelCall, QQBinding, StyleCorpus, Task

logger = logging.getLogger(__name__)
WORKER_TIMEOUT_SECONDS = 60
EXECUTION_LOCK = 7273102


@contextmanager
def task_execution(engine, task_id):
    """A process-held lease distinguishes a slow live call from a dead worker."""
    from procrastinate.exceptions import JobRetry
    from procrastinate.retry import RetryDecision

    params = {"namespace": EXECUTION_LOCK, "task": str(task_id)}
    with engine.connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, hashtext(:task))"), params
        )
        connection.commit()
        if not acquired:
            raise JobRetry(RetryDecision(retry_in={"seconds": 3}))
        try:
            yield
        finally:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, hashtext(:task))"), params
                )
                connection.commit()
            except Exception:
                # A session lock must never return to the connection pool.
                connection.invalidate()
                raise


def _settle_calls(db, calls):
    for call in calls:
        if call.state != "reserved":
            continue
        day = db.scalar(select(BudgetDay).where(BudgetDay.day == call.budget_day).with_for_update())
        if not call.budget_exempt:
            day.reserved_usd -= call.reserved_usd
            day.spent_usd += call.reserved_usd
        call.state, call.charged_usd = "unknown", call.reserved_usd
        call.error, call.finished_at = "worker_interrupted", datetime.now(UTC)


def _clean_inputs(db, task):
    from lisa.style_corpus import clear_corpus
    from lisa.vision import clear_image_inputs

    if task.kind == "chat":
        clear_image_inputs(db, task.id)
        db.execute(delete(ChatMessage).where(ChatMessage.task_id == task.id))
    if task.kind == "image":
        task.input_snapshot = None
    if task.kind == "style_distill":
        for corpus in db.scalars(select(StyleCorpus).where(StyleCorpus.task_id == task.id)):
            clear_corpus(db, corpus)
    elif task.kind == "style_trial":
        task.request = {
            "kind": task.request.get("kind"),
            **({"mode": "refine"} if task.request.get("mode") == "refine" else {}),
        }


def _interruption_notice(db, task):
    from lisa.author import _notice
    from lisa.chat import _outbound

    if task.kind in {"chat", "image"}:
        binding = db.get(QQBinding, task.user_id)
        if binding:
            _outbound(
                db,
                task,
                binding.qq_id,
                "服务中断，这次绘图已停止自动重试，原图仍保留。" if task.kind == "image"
                else "服务中断，这次对话未能完成，已停止自动重试。请重新发送消息继续。",
            )
    elif task.kind in {"scene", "rewrite"}:
        _notice(db, task, "服务中断，这次创作未保存新正文，原文仍在。请在网页查看任务状态。")


# Read only candidates; lock and recheck each one in its own transaction. Live
# tasks own an execution lease even if their heartbeat is delayed.
_CANDIDATES = """
SELECT t.id FROM tasks t
JOIN procrastinate_jobs j ON j.id = t.queue_job_id
LEFT JOIN procrastinate_workers w ON w.id = j.worker_id
WHERE j.task_name IN ('lisa.chat', 'lisa.author', 'lisa.index', 'lisa.style',
    'lisa.contact', 'lisa.life_plan', 'lisa.shared', 'lisa.image')
AND (
    (j.status = 'doing' AND
        (w.id IS NULL OR w.last_heartbeat < now() - make_interval(secs => :timeout)))
    OR (j.status NOT IN ('todo', 'doing') AND
        (t.status IN ('pending', 'running') OR EXISTS
            (SELECT 1 FROM model_calls c WHERE c.task_id = t.id AND c.state = 'reserved')))
    OR (j.status = 'todo' AND t.status = 'running')
)
ORDER BY t.created_at LIMIT 100
"""


def _recover_periodic_jobs(engine) -> int:
    # These jobs have no business Task. Do not replay a historical scan; the next
    # scheduled scan will apply today's permissions, active periods and settings.
    with Session(engine) as db, db.begin():
        jobs = list(
            db.execute(
                text("""
            SELECT j.id, j.task_name, j.args FROM procrastinate_jobs j
            LEFT JOIN procrastinate_workers w ON w.id = j.worker_id
            WHERE j.status = 'doing'
            AND (w.id IS NULL OR w.last_heartbeat < now()-make_interval(secs => :timeout))
            AND j.task_name IN ('lisa.recovery_sweep', 'lisa.style_cleanup',
                'lisa.index_sweep', 'lisa.schedule_sweep', 'lisa.contact_sweep')
            LIMIT 100 FOR UPDATE OF j SKIP LOCKED
        """),
                {"timeout": WORKER_TIMEOUT_SECONDS},
            ).mappings()
        )
        recovered = 0
        for job in jobs:
            key = f"{job['task_name']}:{job['args'].get('timestamp')}"
            if not db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, hashtext(:task))"),
                {"namespace": EXECUTION_LOCK, "task": key},
            ):
                continue
            db.execute(
                text("SELECT procrastinate_finish_job_v1(:id, 'failed', false)"),
                {"id": job["id"]},
            )
            recovered += 1
        return recovered


def recover_tasks(engine) -> int:
    recovered = _recover_periodic_jobs(engine)
    with Session(engine) as db:
        ids = list(db.scalars(text(_CANDIDATES), {"timeout": WORKER_TIMEOUT_SECONDS}))
    for task_id in ids:
        with Session(engine) as db, db.begin():
            if not db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, hashtext(:task))"),
                {"namespace": EXECUTION_LOCK, "task": str(task_id)},
            ):
                continue
            # Shared lock order with reservation, settlement and account deletion.
            db.execute(text("SELECT pg_advisory_xact_lock(7273101)"))
            user_id = db.scalar(select(Task.user_id).where(Task.id == task_id))
            user = db.scalar(select(Account).where(Account.id == user_id).with_for_update())
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None:
                continue
            job = (
                db.execute(
                    text("""
                SELECT j.id, j.status, j.abort_requested, j.attempts,
                    (w.id IS NULL OR w.last_heartbeat <
                        now() - make_interval(secs => :timeout)) AS abandoned
                FROM procrastinate_jobs j
                LEFT JOIN procrastinate_workers w ON w.id = j.worker_id
                WHERE j.id = :id FOR UPDATE OF j
                """),
                    {"id": task.queue_job_id, "timeout": WORKER_TIMEOUT_SECONDS},
                )
                .mappings()
                .one_or_none()
            )
            if job is None or (job["status"] == "doing" and not job["abandoned"]):
                continue
            # A candidate may have finished or been retried since the first read.
            calls = list(db.scalars(select(ModelCall).where(ModelCall.task_id == task.id)))
            if job["status"] == "todo" and task.status != "running":
                continue
            if (
                job["status"] not in {"todo", "doing"}
                and task.status not in {"pending", "running"}
                and not any(call.state == "reserved" for call in calls)
            ):
                continue
            _settle_calls(db, calls)
            if task.adopted_at:
                task.status, task.error = "completed", None
                task.finished_at = task.finished_at or task.adopted_at
            elif task.status in {"pending", "running"}:
                if (
                    (task.user_id is not None and (user is None or not user.active))
                    or job["abort_requested"]
                    or job["status"] in {"aborted", "cancelled"}
                ):
                    task.status, task.error = "cancelled", "worker_interrupted"
                elif not calls and job["status"] in {"doing", "todo"} and job["attempts"] < 3:
                    # No provider attempt was reserved: resume the same task and
                    # scope, letting its executor recheck current permissions.
                    task.status, task.error = "pending", None
                    if job["status"] == "doing":
                        db.execute(
                            text("SELECT procrastinate_retry_job_v2(:id, now(), NULL, NULL, NULL)"),
                            {"id": job["id"]},
                        )
                    recovered += 1
                    continue
                else:
                    unknown = any(call.state == "unknown" for call in calls)
                    task.status = (
                        "outcome_unknown"
                        if unknown and task.kind in {"index", "shared_index", "image"}
                        else "failed"
                    )
                    task.error = "model_unknown" if unknown else "worker_interrupted"
                    if user is not None and user.active:
                        _interruption_notice(db, task)
                task.finished_at = datetime.now(UTC)
            _clean_inputs(db, task)
            if job["status"] in {"doing", "todo"}:
                db.execute(
                    text("SELECT procrastinate_finish_job_v1(:id, :status, false)"),
                    {
                        "id": job["id"],
                        "status": "succeeded"
                        if task.status == "completed"
                        else "aborted"
                        if task.status == "cancelled"
                        else "failed",
                    },
                )
            recovered += 1
            logger.info("task=%s recovery=%s", task.id, task.status)
    return recovered
