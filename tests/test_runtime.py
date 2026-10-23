import asyncio
import logging
import os
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from lisa import worker
from lisa.worker import create_queue


def test_health_and_logs_have_request_id_without_private_inputs(client, caplog):
    with caplog.at_level(logging.INFO, logger="lisa.api"):
        response = client.get("/api/health?private_value=do-not-log-this")
    assert response.json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"] in caplog.text
    assert "do-not-log-this" not in caplog.text


def test_queue_factory_executes_persisted_task(settings, engine):
    queue = create_queue(settings)
    seen = []

    @queue.task(name="synthetic_test_task", queue="lisa_test")
    async def synthetic_task(value: str):
        seen.append(value)

    async def exercise():
        async with queue.open_async():
            job_id = await synthetic_task.defer_async(value="synthetic payload")
            await asyncio.wait_for(
                queue.run_worker_async(
                    queues=["lisa_test"],
                    wait=False,
                    concurrency=1,
                    install_signal_handlers=False,
                ),
                timeout=20,
            )
            return job_id

    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        job_id = runner.run(exercise())
    assert seen == ["synthetic payload"]
    with engine.connect() as connection:
        state = connection.scalar(
            text("SELECT status FROM procrastinate_jobs WHERE id = :id"), {"id": job_id}
        )
    assert state == "succeeded"


@pytest.mark.asyncio
async def test_worker_registers_separate_index_consumer(monkeypatch):
    consumers = []

    class FakeQueue:
        @asynccontextmanager
        async def open_async(self):
            yield

        async def run_worker_async(self, **kwargs):
            consumers.append(kwargs)

    monkeypatch.setattr(worker, "create_queue", FakeQueue)
    await worker.run_worker(once=True)
    assert {(tuple(item["queues"]), item["concurrency"]) for item in consumers} == {
        (("lisa",), 2),
        (("lisa_author",), 1),
        (("lisa_index",), 1),
        (("lisa_style",), 1),
    }
    assert all(not item["wait"] and not item["install_signal_handlers"] for item in consumers)
