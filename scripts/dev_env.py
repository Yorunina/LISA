"""Manage LISA's native Windows development dependencies (not the application)."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import procrastinate
import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PG = RUNTIME / "tools" / "postgresql" / "bin"
DATA = RUNTIME / "postgres" / "data"
NAPCAT = RUNTIME / "tools" / "napcat"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def settings() -> dict[str, str]:
    values = dotenv_values(ROOT / ".env")
    required = (
        "LISA_DB_HOST",
        "LISA_DB_PORT",
        "LISA_DB_NAME",
        "LISA_DB_USER",
        "LISA_DB_PASSWORD",
        "LISA_DB_ADMIN_PASSWORD",
        "LISA_ONEBOT_WS_URL",
        "LISA_ONEBOT_TOKEN",
        "LISA_NAPCAT_WEBUI_TOKEN",
    )
    for key in required:
        if not values.get(key) or values[key] == "generated-by-init":
            raise RuntimeError(f"Missing {key}; run init before starting dependencies")
    return {key: str(values[key]) for key in required}


def conninfo(*, admin: bool = False, maintenance: bool = False) -> str:
    cfg = settings()
    return make_conninfo(
        host=cfg["LISA_DB_HOST"],
        port=cfg["LISA_DB_PORT"],
        dbname="postgres" if maintenance else cfg["LISA_DB_NAME"],
        user="postgres" if admin else cfg["LISA_DB_USER"],
        password=cfg["LISA_DB_ADMIN_PASSWORD" if admin else "LISA_DB_PASSWORD"],
        connect_timeout=5,
    )


def init() -> None:
    env = ROOT / ".env"
    if not env.exists():
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        lines = [
            line.replace("generated-by-init", secrets.token_hex(24))
            if line.endswith("=generated-by-init")
            else line
            for line in template.splitlines()
        ]
        with env.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    cfg = settings()
    config = RUNTIME / "napcat" / "config"
    config.mkdir(parents=True, exist_ok=True)
    files = {
        "webui.json": {
            "host": "127.0.0.1",
            "port": 6099,
            "prefix": "",
            "token": cfg["LISA_NAPCAT_WEBUI_TOKEN"],
            "loginRate": 3,
        },
        "onebot11.json": {
            "network": {
                "httpServers": [],
                "httpSseServers": [],
                "httpClients": [],
                "websocketServers": [
                    {
                        "enable": True,
                        "name": "lisa-local",
                        "host": "127.0.0.1",
                        "port": 3001,
                        "reportSelfMessage": False,
                        "enableForcePushEvent": True,
                        "messagePostFormat": "array",
                        "token": cfg["LISA_ONEBOT_TOKEN"],
                        "debug": False,
                        "heartInterval": 30000,
                    }
                ],
                "websocketClients": [],
                "plugins": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        },
    }
    default_config = NAPCAT / "napcat" / "config" / "napcat.json"
    if default_config.exists():
        files["napcat.json"] = json.loads(default_config.read_text(encoding="utf-8"))
        files["napcat.json"].update(fileLog=False, consoleLog=False)
    for name, value in files.items():
        path = config / name
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
    configure_napcat_logging()
    print("Local credentials and NapCat configuration ready; diagnostic logging disabled.")


def configure_napcat_logging() -> None:
    config_dir = RUNTIME / "napcat" / "config"
    for path in [config_dir / "napcat.json", *config_dir.glob("napcat_[0-9]*.json")]:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update(fileLog=False, consoleLog=False)
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )


def pg_run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Windows postgres may retain inherited pipe handles after pg_ctl exits.
    # File-backed output avoids waiting forever for a background server to close them.
    log = RUNTIME / "logs" / "postgres-control.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(PG / args[0]), *args[1:]]
    with log.open("ab") as output:
        offset = output.tell()
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            timeout=120,
        )
    with log.open("rb") as output:
        output.seek(offset)
        message = output.read().decode("utf-8", errors="replace")
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, output=message)
    return subprocess.CompletedProcess(command, result.returncode, stdout=message, stderr="")


def db_start() -> None:
    cfg = settings()
    if not (PG / "pg_ctl.exe").exists():
        raise RuntimeError("Run scripts/download_native.py first")
    if not (DATA / "PG_VERSION").exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        password_file = DATA.parent / f"init-password-{secrets.token_hex(8)}.tmp"
        try:
            with password_file.open("x", encoding="utf-8") as handle:
                handle.write(cfg["LISA_DB_ADMIN_PASSWORD"] + "\n")
            pg_run(
                "initdb.exe",
                "-D",
                str(DATA),
                "-U",
                "postgres",
                "--encoding=UTF8",
                "--locale=C",
                "--auth=scram-sha-256",
                f"--pwfile={password_file}",
            )
        finally:
            if password_file.exists():
                password_file.unlink()
    status = pg_run("pg_ctl.exe", "status", "-D", str(DATA), check=False)
    if status.returncode == 3:
        # Refuse to hide an unrelated service behind our configured port.
        import socket

        with socket.socket() as probe:
            if probe.connect_ex((cfg["LISA_DB_HOST"], int(cfg["LISA_DB_PORT"]))) == 0:
                raise RuntimeError("Database port is already occupied by another process")
        # PostgreSQL error text may contain user values; application logs retain anonymous errors.
        pg_run(
            "pg_ctl.exe",
            "start",
            "-D",
            str(DATA),
            "-w",
            "-t",
            "30",
            "-l",
            os.devnull,
            "-o",
            f"-h 127.0.0.1 -p {int(cfg['LISA_DB_PORT'])}",
        )
    elif status.returncode != 0:
        raise RuntimeError(status.stderr or status.stdout)
    print(f"PostgreSQL running on 127.0.0.1:{cfg['LISA_DB_PORT']}")


def db_stop() -> None:
    status = pg_run("pg_ctl.exe", "status", "-D", str(DATA), check=False)
    if status.returncode == 0:
        pg_run("pg_ctl.exe", "stop", "-D", str(DATA), "-m", "fast", "-w", "-t", "30")
        print("PostgreSQL stopped; data preserved.")
    elif status.returncode in (3, 4):
        print("PostgreSQL is not running.")
    else:
        raise RuntimeError(status.stderr or status.stdout)


def privacy_cleanup() -> None:
    """Remove historical dependency log details during a stopped-service maintenance window."""
    status = pg_run("pg_ctl.exe", "status", "-D", str(DATA), check=False)
    if status.returncode not in (3, 4):
        raise RuntimeError("Stop PostgreSQL before cleaning its historical logs")
    pid_file = RUNTIME / "napcat.pid"
    if os.name == "nt" and pid_file.exists():
        pid = int(pid_file.read_text(encoding="ascii").strip())
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ $p.Path }}",
            ],
            capture_output=True,
            text=True,
            check=True,
            creationflags=NO_WINDOW,
        )
        if result.stdout.strip().lower() == str(NAPCAT / "node.exe").lower():
            raise RuntimeError("Run scripts/dev.ps1 StopQQ before cleaning local NapCat logs")

    postgres_log = RUNTIME / "logs" / "postgres.log"
    if postgres_log.exists():
        # DETAIL/STATEMENT and their continuation lines can contain names, QQ IDs and SQL.
        # Error summaries can also quote values, so retain only their timestamp and severity.
        record = re.compile(r"^(\d{4}-\d{2}-\d{2} .*?\[\d+\] )([A-Z]+):\s*(.*)$")
        cleaned = []
        for line in postgres_log.read_text(encoding="utf-8", errors="replace").splitlines():
            match = record.match(line)
            if not match:
                continue
            prefix, level, message = match.groups()
            if level in {"DETAIL", "STATEMENT", "CONTEXT", "QUERY", "HINT"}:
                continue
            if level in {"ERROR", "FATAL", "PANIC", "WARNING"}:
                message = "Historical database error details removed."
            cleaned.append(f"{prefix}{level}:  {message}")
        postgres_log.write_text("\n".join(cleaned) + "\n", encoding="utf-8")

    configure_napcat_logging()
    # These are dependency diagnostic logs only; QQ session and application data are untouched.
    napcat_logs = [
        *(RUNTIME / "logs").glob("napcat*.log"),
        *(RUNTIME / "napcat" / "logs").glob("*.log"),
    ]
    for path in napcat_logs:
        path.write_text("", encoding="utf-8")
    print("Historical dependency log details removed; NapCat diagnostic logging disabled.")


def db_init() -> None:
    cfg = settings()
    with psycopg.connect(conninfo(admin=True, maintenance=True), autocommit=True) as conn:
        user = cfg["LISA_DB_USER"]
        database = cfg["LISA_DB_NAME"]
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone():
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(user),
                    sql.Literal(cfg["LISA_DB_PASSWORD"]),
                )
            )
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database),
                    sql.Identifier(user),
                )
            )
    with psycopg.connect(conninfo(admin=True), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with psycopg.connect(conninfo()) as conn:
        exists = conn.execute("SELECT to_regclass('procrastinate_jobs')").fetchone()[0]
    if not exists:
        connector = procrastinate.PsycopgConnector(conninfo=conninfo())
        app = procrastinate.App(connector=connector)
        with app.open():
            app.schema_manager.apply_schema()
    print("lisa database, non-superuser role, pgvector and Procrastinate schema ready.")


async def queue_check() -> None:
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo()))
    queue = "lisa_dev_probe_" + secrets.token_hex(8)
    seen = []

    @app.task(name=queue, queue=queue)
    async def probe(value: str) -> None:
        seen.append(value)

    async with app.open_async():
        job_id = await probe.defer_async(value=queue)
        try:
            await asyncio.wait_for(
                app.run_worker_async(
                    queues=[queue],
                    wait=False,
                    concurrency=1,
                    install_signal_handlers=False,
                ),
                timeout=20,
            )
            if seen != [queue]:
                raise RuntimeError("Worker did not execute the probe exactly once")
            with psycopg.connect(conninfo()) as conn:
                state = conn.execute(
                    "SELECT status FROM procrastinate_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
                if not state or state[0] != "succeeded":
                    raise RuntimeError("Worker did not persist successful completion")
        finally:
            with psycopg.connect(conninfo()) as conn:
                conn.execute("DELETE FROM procrastinate_jobs WHERE id = %s", (job_id,))
    print("PASS: persisted task -> Windows worker -> succeeded (probe removed)")


def run_async(coro) -> None:
    # Psycopg async connections require SelectorEventLoop on Windows.
    factory = asyncio.SelectorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(coro)


def check() -> None:
    for package in ("fastapi", "sqlalchemy", "alembic", "pgvector", "procrastinate"):
        print(f"{package}: {importlib.metadata.version(package)}")
    with psycopg.connect(conninfo()) as conn:
        print("PostgreSQL:", conn.execute("SHOW server_version").fetchone()[0])
        print(
            "pgvector:",
            conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
            ).fetchone()[0],
        )
        conn.execute("CREATE TEMP TABLE lisa_probe (id int, embedding vector(3), body jsonb)")
        conn.execute(
            "INSERT INTO lisa_probe VALUES (1, '[1,0,0]', '{\"text\":\"中文创作\"}'), "
            "(2, '[0,1,0]', '{}')",
        )
        row = conn.execute(
            "SELECT id, body->>'text' FROM lisa_probe ORDER BY embedding <-> '[1,0,0]' LIMIT 1",
        ).fetchone()
        if row != (1, "中文创作"):
            raise RuntimeError("Vector/UTF-8/JSONB probe failed")
        if conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            raise RuntimeError("Application role must not be a superuser")
        conn.rollback()
    print("PASS: authenticated non-superuser connection, UTF-8/JSONB, exact vector search")
    run_async(queue_check())


async def qq_check() -> None:
    cfg = settings()
    async with connect(
        cfg["LISA_ONEBOT_WS_URL"],
        additional_headers={"Authorization": f"Bearer {cfg['LISA_ONEBOT_TOKEN']}"},
        open_timeout=5,
        proxy=None,
    ) as ws:
        for action in ("get_version_info", "get_login_info"):
            echo = secrets.token_hex(8)
            await ws.send(json.dumps({"action": action, "params": {}, "echo": echo}))
            async with asyncio.timeout(10):
                while True:
                    response = json.loads(await ws.recv())
                    if response.get("echo") == echo:
                        break
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise RuntimeError(f"OneBot {action} failed")
            print(f"PASS: OneBot {action} (identity details omitted)")


if __name__ == "__main__":
    actions = {
        "init": init,
        "db-start": db_start,
        "db-stop": db_stop,
        "db-init": db_init,
        "privacy-cleanup": privacy_cleanup,
        "check": check,
        "qq-check": lambda: run_async(qq_check()),
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions)
    args = parser.parse_args()
    try:
        actions[args.action]()
    except Exception as exc:
        message = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            message = exc.stderr or exc.stdout or message
        for key, value in dotenv_values(ROOT / ".env").items():
            if value and ("PASSWORD" in key or "TOKEN" in key):
                message = message.replace(value, "<redacted>")
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
