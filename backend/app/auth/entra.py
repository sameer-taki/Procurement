"""Entra ID (Azure AD) SSO via OIDC (Authlib).

Maps Entra app-role/group claims to local role codes
(REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN); the approval limit lives on the
role. The exact app-role/group ids are an OPEN QUESTION (CLAUDE.md §7); the default
mapping matches the local role *name* inside a claim value, case-insensitively, and
falls back to settings.default_role.
"""
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from sqlmodel import Session, select

from ..config import settings
from ..gateway.models import User

ROLE_CODES = ["ADMIN", "APPROVER", "OFFICER", "REQUESTER", "VIEWER"]  # most→least privileged

oauth = OAuth()
_registered = False


def get_oauth() -> OAuth:
    """Register the Entra OIDC client once (lazy; only when configured)."""
    global _registered
    if not _registered:
        oauth.register(
            name="entra",
            client_id=settings.entra_client_id,
            client_secret=settings.entra_client_secret,
            server_metadata_url=(
                f"https://login.microsoftonline.com/{settings.entra_tenant_id}"
                "/v2.0/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": settings.entra_scope},
        )
        _registered = True
    return oauth


def map_role(claims: dict) -> str:
    """Pick the highest-privilege local role named in the configured claim."""
    raw = claims.get(settings.entra_role_claim) or []
    if isinstance(raw, str):
        raw = [raw]
    values = " ".join(str(v) for v in raw).upper()
    for code in ROLE_CODES:                      # ADMIN first, so it wins
        if code in values:
            return code
    return settings.default_role


def upsert_user_from_claims(session: Session, claims: dict) -> Optional[User]:
    """Provision/refresh a local user from verified OIDC claims; None if no email."""
    oid = claims.get("oid") or claims.get("sub")
    email = claims.get("email") or claims.get("preferred_username")
    if not email:
        return None
    name = claims.get("name")

    user = None
    if oid:
        user = session.exec(select(User).where(User.entra_oid == oid)).first()
    if user is None:
        user = session.exec(select(User).where(User.email == email)).first()

    mapped = map_role(claims)
    if user is None:
        user = User(email=email, name=name, entra_oid=oid, role_code=mapped, active=True)
    else:
        user.entra_oid = oid or user.entra_oid
        user.name = name or user.name
        # Don't demote an existing ADMIN via a thin claim set; otherwise track Entra.
        if user.role_code != "ADMIN":
            user.role_code = mapped
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
