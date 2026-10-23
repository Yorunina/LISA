from alembic import context

from lisa.db import make_engine
from lisa.models import Base
from lisa.settings import Settings

target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    # Procrastinate owns its schema; application autogeneration must leave it alone.
    return not (type_ == "table" and name.startswith("procrastinate_"))


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(Settings())
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_name=include_name
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
