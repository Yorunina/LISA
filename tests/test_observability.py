import logging
from datetime import UTC, datetime, timedelta

import pytest

from lisa.models import Account, Task
from lisa.queue import _execute_task


def test_request_logs_use_route_templates_and_elapsed_time(client, caplog):
    private_id = "1305a58b-72df-42f2-a7b2-656633104b89"
    with caplog.at_level(logging.INFO, logger="lisa.api"):
        response = client.get(f"/api/projects/{private_id}?secret=private-query")
        client.get("/api/not-a-route-private-token")
    assert response.status_code == 401
    assert "route=/api/projects/{project_id}" in caplog.text
    assert "duration_ms=" in caplog.text
    assert private_id not in caplog.text
    assert "private-query" not in caplog.text
    assert "not-a-route-private-token" not in caplog.text


@pytest.mark.asyncio
async def test_task_timing_preserves_exception_without_logging_private_text(
    settings, db, caplog,
):
    account = Account(username="timing-reader", password_hash="synthetic-unused")
    db.add(account)
    db.flush()
    task = Task(
        user_id=account.id, kind="chat", source="qq", status="pending",
        created_at=datetime.now(UTC) - timedelta(seconds=2),
        request={"private": "never-log-task-body"},
    )
    db.add(task)
    db.commit()

    async def fail(*args):
        raise RuntimeError("never-log-provider-response")

    with caplog.at_level(logging.INFO, logger="lisa.queue"):
        with pytest.raises(RuntimeError, match="never-log-provider-response"):
            await _execute_task(settings, str(task.id), fail)
    assert "task_age_ms=" in caplog.text
    assert "execution_ms=" in caplog.text
    assert "outcome=RuntimeError" in caplog.text
    assert "never-log" not in caplog.text


@pytest.mark.asyncio
async def test_task_timing_does_not_treat_normal_return_as_business_success(
    settings, db, caplog,
):
    account = Account(username="timing-failed-reader", password_hash="synthetic-unused")
    db.add(account)
    db.flush()
    task = Task(user_id=account.id, kind="chat", status="failed")
    db.add(task)
    db.commit()

    async def handled_failure(*args):
        return None

    with caplog.at_level(logging.INFO, logger="lisa.queue"):
        await _execute_task(settings, str(task.id), handled_failure)
    assert "outcome=returned" in caplog.text
    assert "outcome=succeeded" not in caplog.text
