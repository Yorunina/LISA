import argparse

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lisa.auth import issue_invites
from lisa.db import make_engine
from lisa.models import Account, QQBinding
from lisa.queue import create_queue, enqueue_pending_indexes
from lisa.settings import ROOT, Settings


def grant_admin(db: Session, username: str, qq_id: str) -> Account:
    """Grant an existing, bound account; never create or replace a QQ binding."""
    if not qq_id.isascii() or not qq_id.isdigit() or int(qq_id) <= 0:
        raise ValueError("QQ 号须为有效数字。")
    account = db.scalar(
        select(Account)
        .where(Account.username == username.strip().lower(), Account.active.is_(True))
        .with_for_update()
    )
    binding = db.get(QQBinding, account.id) if account else None
    if account is None or binding is None or binding.qq_id != qq_id:
        raise ValueError("账号不存在、未绑定或绑定 QQ 不匹配；管理员权限未更改。")
    account.is_admin = True
    return account


def main() -> None:
    parser = argparse.ArgumentParser(description="LISA application management")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("migrate", help="Apply application and queue migrations")
    commands.add_parser("index", help="Queue changed content for indexing; no model calls here")
    invite = commands.add_parser("invite", help="Issue one-time invitation codes (shown once)")
    invite.add_argument("--count", type=int, default=1)
    admin = commands.add_parser(
        "admin", help="Grant admin to an existing matching QQ-bound account"
    )
    admin.add_argument("--username", required=True)
    admin.add_argument("--qq-id", required=True)
    args = parser.parse_args()
    if args.action == "migrate":
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = make_engine(Settings())
        try:
            with engine.connect() as connection:
                exists = connection.scalar(text("SELECT to_regclass('procrastinate_jobs')"))
            if not exists:
                queue = create_queue()
                with queue.open():
                    queue.schema_manager.apply_schema()
        finally:
            engine.dispose()
        print("Application and queue migrations applied.")
    elif args.action == "index":
        settings = Settings()
        engine = make_engine(settings)
        try:
            with Session(engine) as db, db.begin():
                count = enqueue_pending_indexes(db, settings)
            print(f"Queued {count} indexing tasks. No model calls were made by this command.")
        finally:
            engine.dispose()
    elif args.action == "admin":
        engine = make_engine(Settings())
        try:
            with Session(engine) as db, db.begin():
                account = grant_admin(db, args.username, args.qq_id)
                username = account.username
            print(f"Administrator access granted to existing account: {username}.")
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
        finally:
            engine.dispose()
    else:
        engine = make_engine(Settings())
        try:
            with Session(engine) as db, db.begin():
                codes = issue_invites(db, args.count)
            print("One-time invitation codes (store privately):")
            print("\n".join(codes))
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
