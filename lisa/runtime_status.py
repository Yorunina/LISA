"""Small best-effort process heartbeats; no message or request bodies."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lisa.models import RagEvaluationRun, RetrievalObservation, RuntimeHeartbeat

logger = logging.getLogger(__name__)
DETAIL_KEYS = {
    "connected",
    "logged_in",
    "last_received_at",
    "last_sent_at",
    "requests",
    "errors",
    "latency_median_ms",
    "latency_p95_ms",
    "latency_samples",
    "mode",
    "queues",
    "process_running",
    "webui_reachable",
}


def heartbeat(engine, component, status="ok", details=None):
    """Optional telemetry must not interrupt business work."""
    try:
        safe = {k: v for k, v in (details or {}).items() if k in DETAIL_KEYS}
        with Session(engine) as db, db.begin():
            values = dict(
                component=component, status=status, observed_at=datetime.now(UTC), details=safe
            )
            db.execute(
                insert(RuntimeHeartbeat)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[RuntimeHeartbeat.component],
                    set_=values,
                )
            )
        return True
    except Exception:
        logger.warning("runtime_observation_unavailable component=%s", component)
        return False


def cleanup_observations(engine, days):
    try:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with Session(engine) as db, db.begin():
            db.execute(delete(RetrievalObservation).where(RetrievalObservation.created_at < cutoff))
            db.execute(delete(RagEvaluationRun).where(RagEvaluationRun.created_at < cutoff))
    except Exception:
        logger.warning("runtime_observation_cleanup_unavailable")


@asynccontextmanager
async def process_pulse(engine, settings, components, *, details=None):
    async def pulse():
        last_cleanup = datetime.now(UTC)
        while True:
            for component in components:
                snapshot = details() if details else {}
                await asyncio.to_thread(heartbeat, engine, component, "ok", snapshot)
            if "api" in components and settings.operations_maintainers.strip():
                from lisa.napcat_status import snapshot as napcat_snapshot

                try:
                    observed = await asyncio.to_thread(napcat_snapshot, settings)
                    await asyncio.to_thread(
                        heartbeat, engine, "napcat", observed["status"], observed["details"]
                    )
                except Exception:
                    logger.warning("runtime_observation_unavailable component=napcat")
            if "api" in components and (datetime.now(UTC) - last_cleanup).total_seconds() >= 3600:
                await asyncio.to_thread(
                    cleanup_observations, engine, settings.operations_retention_days
                )
                last_cleanup = datetime.now(UTC)
            await asyncio.sleep(settings.operations_heartbeat_seconds)

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for component in components:
            await asyncio.to_thread(heartbeat, engine, component, "offline", {})


class ApiMeasurements:
    def __init__(self):
        self.requests = 0
        self.errors = 0
        self.latencies = deque(maxlen=1000)

    def record(self, status, elapsed_ms):
        self.requests += 1
        self.errors += status >= 500
        self.latencies.append(elapsed_ms)

    def snapshot(self):
        items = sorted(self.latencies)
        return {
            "requests": self.requests,
            "errors": self.errors,
            "latency_samples": len(items),
            "latency_median_ms": round(items[len(items) // 2], 2) if items else None,
            "latency_p95_ms": round(items[min(len(items) - 1, int(len(items) * 0.95))], 2)
            if items
            else None,
        }
