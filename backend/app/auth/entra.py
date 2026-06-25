"""Entra ID (Azure AD) SSO via OIDC (Authlib).

Maps Entra app-role/group claims to local role codes
(REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN); the approval limit lives on the
role. The exact app-role/group ids are an OPEN QUESTION (CLAUDE.md §7).

Mapping is deliberately *exact*, never substring. Each claim value is resolved
independently by:
  1. an explicit configured map (settings.entra_role_map: claim value / GUID ->
     role code, the recommended production path), then
  2. exact, case-insensitive equality of the whole claim value against a canonical
     role code ("ADMIN", "APPROVER", ...).
We never do `code in joined_string` substring containment — that let group names
like 'Finance-Admins', 'Administrative-Assistants' or 'Non-Admin-Users' silently
escalate to ADMIN. Unmatched claims fall back to settings.default_role.
"""
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from sqlmodel import Session, select

from ..config import settings
from ..gateway.models import User

ROLE_CODES = ["ADMIN", "APPROVER", "OFFICER", "REQUESTER", "VIEWER"]  # most→least privileged


def _role_for_claim_value(value: str) -> Optional[str]:
    """Map one claim value to a role code, exact-match only. None if nothing matches.

    1. An explicit configured mapping (settings.entra_role_map) wins — keyed by the
       raw claim value or its GUID, compared case-insensitively. This is the
       production path for opaque group GUIDs / arbitrary group names.
    2. Otherwise the *whole* claim value must equal a canonical role code
       (case-insensitive). This deliberately rejects names that merely contain a
       role code as a substring ('Finance-Admins', 'Non-Admin-Users', 'GoldenAdmin'),
       which is the privilege-escalation hole being closed.
    """
    norm = str(value).strip().upper()
    role_map = getattr(settings, "entra_role_map", None) or {}
    for key, code in role_map.items():
        if str(key).strip().upper() == norm:
            mapped = str(code).strip().upper()
            if mapped in ROLE_CODES:
                return mapped
    if norm in ROLE_CODES:
        return norm
    return None

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
    """Pick the highest-privilege local role mapped from the configured claim.

    Each claim value is resolved independently via an exact (explicit-map or
    whole-token) match — never substring containment — and the most privileged
    matched role wins. Falls back to settings.default_role when none match.
    """
    raw = claims.get(settings.entra_role_claim) or []
    if isinstance(raw, str):
        raw = [raw]
    matched = {role for v in raw if (role := _role_for_claim_value(v)) is not None}
    for code in ROLE_CODES:                          # ADMIN first, so it wins
        if code in matched:
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
