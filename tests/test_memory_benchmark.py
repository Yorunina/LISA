"""Keep candidate, pool and final-assembly evidence distinct in the memory comparison."""

import json
import os
import sys
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from scripts import benchmark_memory


def test_ledger_keeps_unsettled_cost_distinct(monkeypatch):
    calls = [SimpleNamespace(charged_usd=Decimal("0.012"), input_tokens=24, state="succeeded"),
             SimpleNamespace(charged_usd=None, input_tokens=None, state="reserved")]
    session = SimpleNamespace(scalars=lambda statement: calls)
    monkeypatch.setattr(benchmark_memory, "Session", lambda engine: nullcontext(session))
    result = benchmark_memory._ledger(None)
    assert result["charged_cny"] == "0.012"
    assert result["unsettled_call_count"] == 1
    assert result["states"] == {"reserved": 1, "succeeded": 1}


def test_memory_comparison_separates_top_five_pool_and_final():
    scores = benchmark_memory._scores(
        ["target-a", "target-b"], ["target-a", "target-b"],
        ["target-a", "other-1", "other-2", "other-3", "other-4", "target-b"],
        ["target-b"],
    )
    assert scores["recall_at_5"] == 0.5
    assert scores["precision_at_5"] == 0.2
    assert scores["pool_coverage"] == 1
    assert scores["context_coverage"] == 0.5


def test_no_answer_has_no_recall_success_and_exposes_false_candidates():
    scores = benchmark_memory._scores([], [], ["unrelated"], ["unrelated"])
    assert scores["recall_at_5"] is None
    assert scores["pool_coverage"] is None
    assert scores["context_coverage"] is None
    assert scores["precision_at_5"] == 0
    assert scores["no_answer_candidate_count"] == 1


def test_default_memory_comparison_has_no_database_or_model_access(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["benchmark_memory.py"])

    def unexpected_database(*args, **kwargs):
        raise AssertionError("Offline evaluation must not open a database")

    monkeypatch.setattr(benchmark_memory, "make_engine", unexpected_database)
    benchmark_memory.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "offline_validated"
    assert result["model_calls"] == 0
    assert result["database_access"] is False


@pytest.mark.parametrize("fail", [False, True])
def test_live_runner_preserves_environment_and_removes_temporary_database(
    monkeypatch, tmp_path, engine, fail
):
    original_database = os.environ["LISA_DB_NAME"]
    temporary_database = None
    output = tmp_path / "memory-report.json"
    monkeypatch.setattr(sys, "argv", ["benchmark_memory.py", "--live", "--output", str(output)])

    async def comparison(settings, db_engine, data):
        nonlocal temporary_database
        temporary_database = settings.db_name
        assert temporary_database != original_database
        assert os.environ["LISA_DB_NAME"] == original_database
        if fail:
            raise RuntimeError("Synthetic evaluation failure")
        return {"status": "completed", "arms": {}}

    monkeypatch.setattr(benchmark_memory, "run_comparison", comparison)
    if fail:
        with pytest.raises(SystemExit, match="1"):
            benchmark_memory.main()
    else:
        benchmark_memory.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == ("failed" if fail else "completed")
    assert report["isolated_database_removed"] is True
    assert report["ledger"]["call_count"] == 0
    assert os.environ["LISA_DB_NAME"] == original_database
    with engine.connect() as db:
        assert db.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"),
                         {"name": temporary_database}) is None
