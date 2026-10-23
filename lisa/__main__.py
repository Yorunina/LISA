import argparse

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from lisa.auth import issue_invites
from lisa.db import make_engine
from lisa.queue import create_queue, enqueue_pending_indexes
from lisa.settings import ROOT, Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="LISA application management")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("migrate", help="Apply application and queue migrations")
    commands.add_parser("index", help="Queue changed content for indexing; no model calls here")
    invite = commands.add_parser("invite", help="Issue one-time invitation codes (shown once)")
    invite.add_argument("--count", type=int, default=1)
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
