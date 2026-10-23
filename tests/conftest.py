"""Integration tests run only inside scripts/test_app.py's disposable database."""

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from lisa.app import create_app
from lisa.auth import issue_invites
from lisa.models import Base
from lisa.settings import Settings


@pytest.fixture(scope="session")
def settings():
    database = os.environ.get("LISA_DB_NAME", "")
    if not re.fullmatch(r"lisa_test_[0-9a-f]{32}", database):
        pytest.exit("Use: python scripts/test_app.py (isolated database required)", returncode=2)
    return Settings()


@pytest.fixture(scope="session")
def engine(settings):
    engine = create_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(engine):
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(delete(table))


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def invites(db):
    tokens = issue_invites(db, 3)
    db.commit()
    return tokens
