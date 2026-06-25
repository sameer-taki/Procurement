"""Test harness: an isolated SQLite DB with the schema + demo catalog seeded.

CI runs without Postgres, so tests build the schema directly (create_all) on an
in-memory SQLite engine and override get_session — no migrations, no scheduler.
Env is set before any app import so the singleton Settings picks it up.
"""
import os

os.environ["APP_ENV"] = "test"                       # non-prod => non-secure cookies over http
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"
os.environ["STOCK_REFRESH_ENABLED"] = "false"
os.environ["OUTBOX_PROCESS_ENABLED"] = "false"
os.environ["SEED_DEMO_ON_EMPTY"] = "false"
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789-0123456789-abcd")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session, seed_roles_and_admin
from app.domain import stock_service
from app.main import app


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_roles_and_admin(s)
        stock_service.refresh_all(s)        # seed demo catalog + stock
    return engine


@pytest.fixture(name="client")
def client_fixture(engine):
    def _get_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture(name="admin_client")
def admin_client_fixture(client):
    r = client.post("/auth/admin-login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return client
