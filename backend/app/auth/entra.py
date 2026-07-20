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
from .roles import ROLE_CODES, map_roles, role_for_claim_value


def _role_for_claim_value(value: str) -> Optional[str]:
    """Map one Entra claim value to a role code (exact-match only) via the shared
    mapper, using settings.entra_role_map. See app.auth.roles for the guarantee."""
    return role_for_claim_value(value, getattr(settings, "entra_role_map", None) or {})

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
    """Pick the highest-privilege local role mapped from the configured Entra claim.

    Each claim value is resolved independently via an exact (explicit-map or
    whole-token) match — never substring containment — and the most privileged
    matched role wins. Falls back to settings.default_role when none match.
    """
    raw = claims.get(settings.entra_role_claim) or []
    if isinstance(raw, str):
        raw = [raw]
    return map_roles(raw, getattr(settings, "entra_role_map", None) or {}, settings.default_role)


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
    # Does this token actually carry role information? A thin claim set (no roles
    # claim at all) must not silently demote anyone — but a token that DOES carry
    # roles is Entra asserting the user's current entitlements, so honour it even
    # when that means removing ADMIN (the deprovisioning path: an admin revoked in
    # Entra loses it here on next login, not only via an in-app edit).
    has_role_claim = bool(claims.get(settings.entra_role_claim))
    if user is None:
        user = User(email=email, name=name, entra_oid=oid, role_code=mapped, active=True)
    else:
        user.entra_oid = oid or user.entra_oid
        user.name = name or user.name
        if has_role_claim:
            user.role_code = mapped
        # else: preserve the current role — the IdP told us nothing this time.
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
