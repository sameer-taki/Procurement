"""Auth dependencies: resolve the signed-session user and gate by role."""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

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


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> CurrentUser:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = session.get(User, uid)
    if user is None or not user.active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown or inactive user")
    limit = None
    if user.role_code:
        role = session.get(Role, user.role_code)
        limit = role.approval_limit if role else None
    return CurrentUser(
        id=user.id, email=user.email, name=user.name,
        role_code=user.role_code, approval_limit=limit,
    )


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
