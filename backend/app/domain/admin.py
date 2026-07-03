"""Admin panel — user/role management + system health. ADMIN only.

Users auto-provision through Entra SSO with DEFAULT_ROLE (auth/entra.py); this
module is how an admin promotes them afterwards without touching the database:
list users, change a role, deactivate a leaver, and adjust the per-role approval
limits the tiered-approval engine routes by (CLAUDE.md §3: "approval limit lives
on the role"). Every change is audited as an OrderEvent (entity_kind USER/ROLE)
so who-changed-what is answerable later.

Safety rail: the last active ADMIN can neither be demoted nor deactivated — a
tenant with zero admins is unrecoverable from inside the app.

System health exposes what an admin otherwise needs a shell for: which
integrations are live vs demo, the scheduler settings, and the integration
outbox (with a retry for FAILED rows — the recovery path the outbox design
deliberately leaves to a human after max attempts).
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, require_admin
from ..config import settings
from ..db import get_session
from ..gateway.models import IntegrationOutbox, OrderEvent, Role, User

router = APIRouter(prefix="/api/admin", tags=["admin"])

USER_ENTITY_KIND = "USER"
ROLE_ENTITY_KIND = "ROLE"
SYSTEM_ENTITY_KIND = "SYSTEM"

# How many FAILED outbox rows the system view lists (newest first).
FAILED_ROWS_SHOWN = 20


def _audit(session: Session, *, entity_kind: str, entity_id: str,
           event_type: str, actor: str, detail: dict) -> None:
    session.add(OrderEvent(
        entity_kind=entity_kind, entity_id=entity_id,
        event_type=event_type, actor=actor, detail_json=json.dumps(detail),
    ))


def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role_code,
        "active": u.active,
        "entra_linked": bool(u.entra_oid),
    }


def _other_active_admin_exists(session: Session, user_id: str) -> bool:
    return session.exec(
        select(User).where(
            User.role_code == "ADMIN",
            User.active == True,  # noqa: E712
            User.id != user_id,
        )
    ).first() is not None


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_admin),
):
    users = session.exec(select(User).order_by(User.email)).all()
    return [_user_out(u) for u in users]


class UserPatchIn(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: UserPatchIn,
    session: Session = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown user")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No fields to update")

    if "role" in changes:
        if changes["role"] is None or session.get(Role, changes["role"]) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Unknown role: {changes['role']}")
    # The last active ADMIN is untouchable: demoting or deactivating them leaves
    # the tenant with no one able to administer it.
    losing_admin = (
        user.role_code == "ADMIN" and user.active
        and (changes.get("role", "ADMIN") != "ADMIN" or changes.get("active") is False)
    )
    if losing_admin and not _other_active_admin_exists(session, user.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot demote or deactivate the last active admin",
        )

    detail = {"before": {"role": user.role_code, "active": user.active}}
    if "role" in changes:
        user.role_code = changes["role"]
    if "active" in changes:
        user.active = changes["active"]
    detail["after"] = {"role": user.role_code, "active": user.active}
    session.add(user)
    _audit(session, entity_kind=USER_ENTITY_KIND, entity_id=user.id,
           event_type="USER_UPDATED", actor=admin.email, detail=detail)
    session.commit()
    session.refresh(user)
    return _user_out(user)


# --------------------------------------------------------------------------- #
# Roles (approval limits — what the tiered approval engine routes by)
# --------------------------------------------------------------------------- #
@router.get("/roles")
def list_roles(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_admin),
):
    roles = session.exec(select(Role).order_by(Role.code)).all()
    return [{"code": r.code, "name": r.name, "approval_limit": r.approval_limit}
            for r in roles]


class RolePatchIn(BaseModel):
    # None = unlimited (how the ADMIN role is seeded); ge=0 rejects nonsense.
    approval_limit: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


@router.patch("/roles/{code}")
def update_role(
    code: str,
    body: RolePatchIn,
    session: Session = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    role = session.get(Role, code)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown role")
    before = role.approval_limit
    role.approval_limit = body.approval_limit
    session.add(role)
    _audit(session, entity_kind=ROLE_ENTITY_KIND, entity_id=role.code,
           event_type="ROLE_LIMIT_UPDATED", actor=admin.email,
           detail={"before": before, "after": role.approval_limit})
    session.commit()
    return {"code": role.code, "name": role.name,
            "approval_limit": role.approval_limit}


# --------------------------------------------------------------------------- #
# System health: integrations, schedulers, outbox
# --------------------------------------------------------------------------- #
@router.get("/system")
def system_health(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_admin),
):
    from ..gateway.bc import BCAdapter
    from ..gateway.accura import AccuraAdapter
    from ..gateway.kiwiplan import KiwiplanAdapter

    integrations = [
        {"system": "Business Central", "configured": settings.bc_enabled,
         "mode": "demo" if BCAdapter().use_fakes else "live"},
        {"system": "Kiwiplan", "configured": settings.kiwiplan_enabled,
         "mode": "demo" if KiwiplanAdapter().use_fakes else "live"},
        {"system": "Accura", "configured": settings.accura_enabled,
         "mode": "demo" if AccuraAdapter().use_fakes else "live"},
        {"system": "Entra ID SSO", "configured": settings.entra_enabled,
         "mode": "live" if settings.entra_enabled else "off"},
        {"system": "Graph mailer", "configured": settings.graph_enabled,
         "mode": "live" if settings.graph_enabled else "off"},
        {"system": "Analytics warehouse", "configured": settings.warehouse_enabled,
         "mode": "live" if settings.warehouse_enabled else "off"},
    ]
    schedulers = [
        {"job": "stock_refresh", "enabled": settings.stock_refresh_enabled,
         "interval_seconds": settings.stock_refresh_seconds},
        {"job": "outbox_process", "enabled": settings.outbox_process_enabled,
         "interval_seconds": settings.outbox_process_seconds},
        {"job": "usage_import", "enabled": settings.usage_import_enabled,
         "interval_seconds": settings.usage_import_seconds},
    ]

    rows = session.exec(select(IntegrationOutbox)).all()
    counts = {"PENDING": 0, "SENDING": 0, "SENT": 0, "FAILED": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    failed = sorted(
        (r for r in rows if r.status == "FAILED"),
        key=lambda r: r.created_at, reverse=True,
    )[:FAILED_ROWS_SHOWN]
    return {
        "integrations": integrations,
        "schedulers": schedulers,
        "outbox": {
            "counts": counts,
            "failed_rows": [{
                "id": r.id,
                "action": r.action,
                "entity_ref": r.entity_ref,
                "attempts": r.attempts,
                "last_error": r.last_error,
                "created_at": r.created_at.isoformat(),
            } for r in failed],
        },
    }


@router.post("/outbox/{row_id}/retry")
def retry_outbox_row(
    row_id: int,
    session: Session = Depends(get_session),
    admin: CurrentUser = Depends(require_admin),
):
    """Re-queue one FAILED outbox row (the human recovery path after max
    attempts). Attempts reset so the processor gives it a full fresh budget.

    Guard: the partial unique index only allows one LIVE row per (target,
    action, entity_ref) — if something already re-enqueued this work, flipping
    the dead row back would collide, so refuse instead."""
    row = session.get(IntegrationOutbox, row_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown outbox row")
    if row.status != "FAILED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only FAILED rows can be retried (row is {row.status})",
        )
    live = session.exec(
        select(IntegrationOutbox).where(
            IntegrationOutbox.target == row.target,
            IntegrationOutbox.action == row.action,
            IntegrationOutbox.entity_ref == row.entity_ref,
            IntegrationOutbox.status != "FAILED",
        )
    ).first()
    if live is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A live outbox row (#{live.id}, {live.status}) already covers this work",
        )
    row.status = "PENDING"
    row.attempts = 0
    session.add(row)
    _audit(session, entity_kind=SYSTEM_ENTITY_KIND, entity_id=f"outbox:{row.id}",
           event_type="OUTBOX_RETRIED", actor=admin.email,
           detail={"action": row.action, "entity_ref": row.entity_ref})
    session.commit()

    from . import purchasing
    result = purchasing.process_outbox(session)
    return {"requeued": row.id, "processed": result}
