"""Auth dependencies: resolve the current user and gate by role.

Two sign-in paths are accepted, tried in order:
  1. a Clerk session JWT in `Authorization: Bearer <token>` (the cloud path —
     the Vercel frontend signs in via Clerk / federated Microsoft); and
  2. the signed session cookie (break-glass admin login and legacy Entra OIDC).

Whichever resolves first wins; the rest of the app's RBAC (role_code /
approval_limit) is identical regardless of how the user authenticated.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from ..config import settings
from ..db import get_session
from ..gateway.models import Role, User

MUTATOR_ROLES = {"OFFICER", "ADMIN"}   # CLAUDE.md P1: only OFFICER/ADMIN mutate


@dataclass
class CurrentUser:
    id: str
    email: str
    name: Optional[str]
    role_code: Optional[str]
    approval_limit: Optional[float]

    @property
    def is_admin(self) -> bool:
        return self.role_code == "ADMIN"

    @property
    def can_mutate(self) -> bool:
        return self.role_code in MUTATOR_ROLES


def _to_current_user(session: Session, user: User) -> CurrentUser:
    limit = None
    if user.role_code:
        role = session.get(Role, user.role_code)
        limit = role.approval_limit if role else None
    return CurrentUser(
        id=user.id, email=user.email, name=user.name,
        role_code=user.role_code, approval_limit=limit,
    )


def _bearer_token(request: Request) -> Optional[str]:
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        token = authz[7:].strip()
        return token or None
    return None


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> CurrentUser:
    # 1) Clerk bearer token (cloud path).
    token = _bearer_token(request)
    if token and settings.clerk_enabled:
        from . import clerk
        try:
            claims = clerk.verify_token(token)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
            )
        user = clerk.upsert_user_from_clerk(session, claims)
        if user is None or not user.active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown or inactive user"
            )
        return _to_current_user(session, user)

    # 2) Signed session cookie (break-glass admin / legacy Entra OIDC).
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = session.get(User, uid)
    if user is None or not user.active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown or inactive user")
    return _to_current_user(session, user)


def require_roles(*codes: str):
    """Dependency factory: require the user to hold one of `codes`."""
    allowed = set(codes)

    def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role_code not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(sorted(allowed))}",
            )
        return user

    return _dep


require_admin = require_roles("ADMIN")
require_mutator = require_roles(*MUTATOR_ROLES)
