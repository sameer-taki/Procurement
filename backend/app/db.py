"""Engine + session + startup migrate/seed.

The gateway owns canonical state in this DB. In production the schema is managed
by Alembic (`alembic upgrade head` on startup); tests build the schema directly
against an in-memory SQLite engine and override `get_session`.
"""
import os
from typing import Iterator

from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from .config import settings

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend


def _connect_args(url: str) -> dict:
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


engine = create_engine(
    settings.database_url,
    connect_args=_connect_args(settings.database_url),
    pool_pre_ping=True,
)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def run_migrations() -> None:
    """Apply Alembic migrations up to head against the configured database."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "app", "alembic"))
    cfg.set_main_option("sqlalchemy.url", make_url(settings.database_url).render_as_string(hide_password=False))
    command.upgrade(cfg, "head")


# Role seed. Approval limits are placeholders — the real tiered thresholds are an
# open question (CLAUDE.md §7) and get finalised in Phase 2. None == unlimited.
DEFAULT_ROLES = [
    ("ADMIN", "Administrator", None),
    ("APPROVER", "Approver", 50000.0),
    ("OFFICER", "Procurement Officer", 5000.0),
    ("REQUESTER", "Requester", 0.0),
    ("VIEWER", "Viewer", 0.0),
]


def seed_roles_and_admin(session: Session) -> None:
    """Idempotent: ensure the five roles and the bootstrap admin user exist."""
    from .gateway.models import Role, User

    for code, name, limit in DEFAULT_ROLES:
        if session.get(Role, code) is None:
            session.add(Role(code=code, name=name, approval_limit=limit))
    session.commit()

    admin_email = settings.first_admin_email or settings.first_admin_username
    existing = session.exec(select(User).where(User.email == admin_email)).first()
    if existing is None:
        session.add(User(
            email=admin_email,
            name=settings.first_admin_name,
            role_code="ADMIN",
            active=True,
        ))
        session.commit()
    elif existing.role_code != "ADMIN":
        existing.role_code = "ADMIN"
        session.add(existing)
        session.commit()
