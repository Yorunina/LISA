"""Temporary research budget with zero-cost production concurrency leases; no worker or QQ."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lisa import model_calls as production_calls  # noqa: E402
from lisa.db import make_engine  # noqa: E402
from lisa.models import Account, BudgetDay, Task  # noqa: E402
from lisa.settings import Settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=(
            "check",
            "develop-review",
            "grade-missing",
            "confirm",
            "native",
            "focus",
            "native-resume",
            "probe",
            "diagnose",
        ),
        required=True,
    )
    parser.add_argument("--mode", default="corpus")
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    # Reject retired workflows before connecting to either database or reserving
    # a production concurrency slot. Their review resource was deliberately removed.
    if args.stage in {"develop-review", "focus"} or (
        args.mode == "review" and args.stage in {"confirm", "native", "probe"}
    ):
        parser.error("review workflows are retired; use the historical frozen inputs")
    production = Settings()
    production_engine = make_engine(production)
    values = dotenv_values(ROOT / "runtime/.env")
    admin_info = make_conninfo(
        host=production.db_host,
        port=production.db_port,
        dbname="postgres",
        user="postgres",
        password=values["LISA_DB_ADMIN_PASSWORD"],
    )
    database = "lisa_skill_eval_" + uuid4().hex
    assert re.fullmatch(r"lisa_skill_eval_[0-9a-f]{32}", database)
    overrides = {
        "LISA_DB_NAME": database,
        "LISA_CHAT_ENABLED": "false",
        "LISA_CHAT_TEST_ENABLED": "false",
        "LISA_AUTONOMY_ENABLED": "false",
        "LISA_PROACTIVE_ENABLED": "false",
        "LISA_EMBEDDING_MODEL": "",
    }
    previous_env = {k: os.environ.get(k) for k in overrides}
    gate_uid, run, lease_count, wait_seconds = None, None, 0, 0.0
    with psycopg.connect(admin_info, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(production.db_user)
            )
        )
        try:
            with psycopg.connect(
                make_conninfo(admin_info, dbname=database), autocommit=True
            ) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            os.environ.update(overrides)
            migrated = subprocess.run(
                [sys.executable, "-m", "lisa", "migrate"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if migrated.returncode:
                raise RuntimeError("isolated_evaluation_migration_failed")
            with Session(production_engine) as db, db.begin():
                user = Account(username="skillgate_" + uuid4().hex[:18], password_hash="!")
                db.add(user)
                db.flush()
                gate_uid = user.id
            if args.stage == "native-resume":
                from scripts.resume_dialogue_skill_native import load_frozen_modules

                assert args.source is not None
                args.source = args.source.resolve()
                args.source.relative_to(ROOT / "runtime/reports/real-chat-prompt")
                load_frozen_modules(args.source)
            from scripts.benchmark_dialogue_skill import DATA, SKILL_ROOT, SkillRun, sha

            original_call = SkillRun.call

            async def gated_call(self, *call_args, **kwargs):
                nonlocal lease_count, wait_seconds
                with Session(production_engine) as db, db.begin():
                    task = Task(
                        user_id=gate_uid,
                        kind="model_probe",
                        source="development",
                        status="running",
                        started_at=datetime.now(UTC),
                        request={"bookkeeping_only": "isolated_evaluation_concurrency"},
                    )
                    db.add(task)
                    db.flush()
                    task_id = task.id
                lease = None
                started = time.monotonic()
                failure = None
                try:
                    # This reservation can never spend money. Actual estimates are
                    # recorded by the ordinary caller in the disposable research DB.
                    gate_settings = production.model_copy(
                        update={"model_daily_usd": Decimal("1000")}
                    )
                    while lease is None:
                        try:
                            lease = production_calls._reserve(
                                gate_settings,
                                production_engine,
                                task_id,
                                gate_uid,
                                Decimal(0),
                                configuration={
                                    "bookkeeping_only": True,
                                    "provider_request": False,
                                    "evaluation_database": database,
                                },
                            )
                        except production_calls.ModelBusy:
                            if time.monotonic() - started >= 120:
                                raise
                            await asyncio.sleep(1)
                    wait_seconds += time.monotonic() - started
                    lease_count += 1
                    key = str(Path(__file__).resolve().relative_to(ROOT))
                    if key not in self.hashes:
                        self.hashes[key] = sha(Path(__file__).read_bytes())
                        target = self.folder / "inputs" / key
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(Path(__file__).read_bytes())
                    self.report["isolated_evaluation"] = {
                        "database": database,
                        "production_daily_limit_unchanged": True,
                        "production_concurrency_lease": "zero_cost_per_logical_call",
                        "runner_sha256": self.hashes[key],
                    }
                    return await original_call(self, *call_args, **kwargs)
                except BaseException as exc:
                    failure = type(exc).__name__
                    raise
                finally:
                    if lease is not None:
                        production_calls._finish(
                            production_engine,
                            lease,
                            state="failed" if failure else "succeeded",
                            charged=Decimal(0),
                            input_tokens=0,
                            output_tokens=0,
                            error=failure,
                        )
                    with Session(production_engine) as db, db.begin():
                        task = db.get(Task, task_id)
                        task.status = "failed" if failure else "completed"
                        task.error, task.finished_at = failure, datetime.now(UTC)

            with patch.object(SkillRun, "call", gated_call):
                if args.stage == "check":
                    engine = make_engine(Settings())
                    with Session(engine) as db:
                        assert db.scalar(select(Account.id).limit(1)) is None
                    engine.dispose()
                    print("Isolated schema ready; no provider requests.", flush=True)
                elif args.stage == "develop-review":
                    from scripts.benchmark_dialogue_skill_review import execute

                    assert args.source is not None
                    run = asyncio.run(
                        execute(args.source.resolve(), args.mode, controls=["previous"])
                    )
                elif args.stage == "grade-missing":
                    from scripts.resume_dialogue_skill_grading import resume

                    assert args.source is not None
                    args.source.resolve().relative_to(ROOT / "runtime/reports/real-chat-prompt")
                    run = asyncio.run(resume(args.source.resolve()))
                elif args.stage == "diagnose":
                    from scripts.diagnose_dialogue_transport import diagnose

                    run = asyncio.run(diagnose(output_budget=args.mode == "budget"))
                elif args.stage == "native-resume":
                    from scripts.resume_dialogue_skill_native import resume

                    run = asyncio.run(resume(args.source))
                elif args.stage == "focus":
                    from scripts.benchmark_dialogue_focus import execute

                    run = asyncio.run(execute("v3" if args.mode == "v3" else "v2"))
                elif args.stage in {"confirm", "native"}:
                    settings = SimpleNamespace(
                        stage="confirm",
                        modes=[args.mode],
                        max_calls=300 if args.mode == "review" else 180,
                        max_cost=Decimal("20") if args.stage == "native" else Decimal("10"),
                        judge_output_tokens=6000,
                        model=production.model_name,
                        reuse_baseline=None,
                        prompt=SKILL_ROOT / "SKILL.md",
                    )
                    datasets = [
                        json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
                        for n in ("develop", "confirm", "guards")
                    ]
                    if args.stage == "native":
                        from scripts.benchmark_dialogue_skill_native import NativeSkillRun

                        run = NativeSkillRun(settings, *datasets)
                        run.settings = run.settings.model_copy(
                            update={"model_daily_usd": Decimal("20")}
                        )
                    else:
                        run = SkillRun(settings, *datasets)
                    asyncio.run(run.execute())
                else:
                    from scripts.probe_dialogue_skill_integration import probe

                    run = asyncio.run(probe(args.mode))
            if run is not None:
                engine = make_engine(Settings())
                with Session(engine) as db:
                    days = db.scalars(select(BudgetDay)).all()
                    spent = sum((d.spent_usd for d in days), Decimal(0))
                    reserved = sum((d.reserved_usd for d in days), Decimal(0))
                engine.dispose()
                run.report["isolated_evaluation"].update(
                    estimated_cny=str(spent + reserved),
                    settled_estimate_cny=str(spent),
                    reserved_cny=str(reserved),
                    concurrency_leases=lease_count,
                    production_charged_cny="0",
                    total_concurrency_wait_seconds=round(wait_seconds, 3),
                    database_removed=False,
                )
                run.save()
        finally:
            if gate_uid is not None:
                with Session(production_engine) as db, db.begin():
                    db.execute(delete(Account).where(Account.id == gate_uid))
            production_engine.dispose()
            assert re.fullmatch(r"lisa_skill_eval_[0-9a-f]{32}", database)
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            if run is not None and "isolated_evaluation" in run.report:
                run.report["isolated_evaluation"]["database_removed"] = True
                run.save()
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            print("Removed isolated evaluation database and concurrency account.", flush=True)


if __name__ == "__main__":
    main()
