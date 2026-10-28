"""Run real application checks in a temporary PostgreSQL database, then remove it."""

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(*args: str, env: dict[str, str]) -> None:
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    result.check_returncode()


def seed_browser_invites(env: dict[str, str]) -> None:
    # Import only after the isolated database is selected.
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from lisa.auth import issue_invites
    from lisa.models import (
        Account,
        Base,
        Chapter,
        Invite,
        Persona,
        PersonaVersion,
        Project,
        QQBinding,
    )
    from lisa.onboarding import issue_code
    from lisa.security import hash_password, hash_token
    from lisa.settings import Settings

    engine = create_engine(Settings().database_url)
    try:
        with Session(engine) as session:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(delete(table))
            codes = issue_invites(session, 7)
            recovery_user = Account(
                username="recovery_reader",
                password_hash=hash_password("synthetic-password-2026"),
            )
            session.add(recovery_user)
            session.flush()
            session.get(Invite, hash_token(codes[3])).used_by = recovery_user.id
            session.add(QQBinding(user_id=recovery_user.id, qq_id="700000003"))
            persona = Persona(
                user_id=recovery_user.id,
                name="留存的小莎",
                prompt="合成测试：找回账号后保留这份性格。",
                version=1,
            )
            session.add(persona)
            session.flush()
            session.add(
                PersonaVersion(
                    persona_id=persona.id, version=1, name=persona.name, prompt=persona.prompt
                )
            )
            styles_user = Account(
                username="styles_reader",
                password_hash=hash_password("synthetic-password-2026"),
            )
            session.add(styles_user)
            session.flush()
            session.get(Invite, hash_token(codes[5])).used_by = styles_user.id
            public_user = Account(username="public_fixture", password_hash="unused-synthetic")
            session.add(public_user)
            session.flush()
            public_work = Project(
                user_id=public_user.id, title="雨夜来信", share_token="r" * 43,
                worldbook="不公开的设定资料",
            )
            session.add(public_work)
            session.flush()
            from lisa.works import paragraph_document

            session.add_all([
                Chapter(
                    project_id=public_work.id, title="第一章 · 车站", position=1,
                    content=paragraph_document([
                        "雨停在黄昏之前。她沿着石板路走向旧车站，口袋里的信被反复折过。",
                        "候车室里还亮着一盏灯。窗边的人抬起头，像是已经等了很久。",
                        "“你终于来了。”他轻声说。",
                    ]),
                ),
                Chapter(
                    project_id=public_work.id, title="第二章 · 信", position=2,
                    content=paragraph_document(["她展开了那封信。<script>只是正文，不执行。</script>"]),
                ),
            ])
            session.add(QQBinding(user_id=styles_user.id, qq_id="700000097"))
            styles_persona = Persona(
                user_id=styles_user.id, name="测试小莎", prompt="合成风格验收人格。", version=1
            )
            session.add(styles_persona)
            session.flush()
            session.add(
                PersonaVersion(
                    persona_id=styles_persona.id,
                    version=1,
                    name=styles_persona.name,
                    prompt=styles_persona.prompt,
                )
            )
            env["LISA_TEST_BINDING_CODE"] = issue_code(session, "binding", "700000001")
            env["LISA_TEST_STATUS_BINDING_CODE"] = issue_code(session, "binding", "700000004")
            env["LISA_TEST_RECOVERY_CODE"] = issue_code(
                session, "recovery", "700000003", recovery_user.id
            )
            session.commit()
        env["LISA_TEST_INVITES"] = ",".join(codes)
    finally:
        engine.dispose()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="Also run Chromium auth checks")
    parser.add_argument("--pytest-target", action="append", default=[],
                        help="Optional pytest file; repeat to select related tests")
    parser.add_argument("--browser-target", action="append", default=[],
                        help="Optional Playwright file inside tests/e2e")
    args = parser.parse_args()
    # Environment overrides remain useful for a dedicated local test PostgreSQL server.
    env = {
        **{k: v for k, v in dotenv_values(ROOT / "runtime" / ".env").items() if v},
        **os.environ,
    }
    database = "lisa_test_" + uuid.uuid4().hex
    admin_conninfo = make_conninfo(
        host=env["LISA_DB_HOST"],
        port=env["LISA_DB_PORT"],
        dbname="postgres",
        user="postgres",
        password=env["LISA_DB_ADMIN_PASSWORD"],
        connect_timeout=5,
    )
    env["LISA_DB_NAME"] = database
    env["LISA_COOKIE_SECURE"] = "false"
    env["LISA_TEST_PYTHON"] = sys.executable
    env["LISA_API_PROXY"] = "http://127.0.0.1:8011"
    env["LISA_TEST_BASE_URL"] = "http://127.0.0.1:5174"
    env["LISA_CHAT_ENABLED"] = "false"
    # Test defaults must not follow a developer's live relay transport override.
    # Dedicated protocol tests explicitly exercise Chat Completions as well.
    env["LISA_MODEL_API_PROTOCOL"] = "responses"
    # Skill-specific tests opt in explicitly; never import a live private corpus
    # into the generic isolated test suite or its artificial input-size limits.
    env["LISA_DIALOGUE_SKILL_MODE"] = "off"
    env["LISA_CHAT_TEST_ENABLED"] = "false"
    env["LISA_AUTONOMY_ENABLED"] = "false"
    env["LISA_PROACTIVE_ENABLED"] = "false"
    env["LISA_SHARED_RADAR_ENABLED"] = "false"
    env["LISA_MODEL_API_KEY"] = ""
    env["LISA_IMAGE_API_KEY"] = ""
    env["LISA_IMAGE_MODEL"] = ""
    env["LISA_IMAGE_BASE_URL"] = ""
    env["LISA_EMBEDDING_MODEL"] = ""
    env["LISA_EMBEDDING_BASE_URL"] = ""
    env["LISA_EMBEDDING_API_KEY"] = ""
    env["LISA_EMBEDDING_BATCH_SIZE"] = "32"
    env["LISA_EMBEDDING_INPUT_PER_MILLION"] = "0"
    env["LISA_VISION_MODEL"] = ""
    env["LISA_VISION_API_KEY"] = ""
    env["LISA_ONEBOT_TOKEN"] = ""
    env["PYTHONUTF8"] = "1"
    os.environ.update(env)

    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(env["LISA_DB_USER"])
            )
        )
        try:
            print("Created isolated application test database.", flush=True)
            with psycopg.connect(
                make_conninfo(admin_conninfo, dbname=database), autocommit=True
            ) as test_admin:
                test_admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
            migrations = [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]
            for direction, revision in migrations:
                run(sys.executable, "-m", "alembic", direction, revision, env=env)
            run(sys.executable, "-m", "alembic", "check", env=env)
            run(sys.executable, "-m", "lisa", "migrate", env=env)
            run(sys.executable, "-m", "lisa", "migrate", env=env)
            run(sys.executable, "-m", "lisa", "index", env=env)
            run(sys.executable, "-m", "lisa.worker", "--once", env=env)
            run(sys.executable, "-m", "pytest", "-q", *(args.pytest_target or ["tests"]), env=env)
            if args.browser:
                seed_browser_invites(env)
                run("node", "node_modules/@playwright/test/cli.js", "test",
                    *args.browser_target, env=env)
        finally:
            # Drop only the database name generated locally in this invocation.
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            print("Removed isolated application test database.", flush=True)


if __name__ == "__main__":
    main()
