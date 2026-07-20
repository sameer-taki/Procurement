"""Clerk auth: verify a Clerk session JWT and provision a local user.

This is the primary sign-in path in the cloud setup. The frontend (on Vercel)
authenticates with Clerk — with Microsoft/Entra federated as a Clerk enterprise
connection — and sends the short-lived Clerk session token as
`Authorization: Bearer <jwt>`. Here we:

  1. verify the token's RS256 signature against Clerk's JWKS (networkless after
     the first fetch; the key set is cached), plus issuer / expiry / authorized-
     party checks;
  2. map a role claim to one of our local role codes using the SAME exact-match
     rule as Entra (app.auth.roles) — never substring, so a group like
     'Finance-Admins' can't escalate to ADMIN;
  3. upsert the local `User` keyed by the Clerk user id (`sub`), falling back to
     email, so an existing Entra/break-glass user is matched rather than doubled.

The app remains the sole writer of canonical state (CLAUDE.md §2): Clerk asserts
*identity + entitlement*; role/limit enforcement stays in this app's RBAC.

Setup notes (see CLOUD_DEPLOY.md): the recommended path is a Clerk JWT template /
session-token customization that adds `email`, `name`, and `role` claims. When
those aren't in the token and CLERK_SECRET_KEY is set, we fall back to a cached
Clerk Backend API lookup so a plain default token still resolves.
"""
import threading
import time
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient
from sqlmodel import Session, select

from ..config import settings
from ..gateway.models import User
from .roles import map_roles

_jwks_client: Optional[PyJWKClient] = None
_jwks_lock = threading.Lock()

# Small TTL cache for the optional Clerk Backend API user lookup, so a burst of
# requests from the same freshly-seen user isn't one API call each.
_api_cache: dict[str, tuple[float, dict]] = {}
_API_TTL = 300.0


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        with _jwks_lock:
            if _jwks_client is None:
                _jwks_client = PyJWKClient(settings.clerk_jwks, cache_keys=True)
    return _jwks_client


def _signing_key_for(token: str):
    """Resolve the RSA public key for a token's `kid` from Clerk's JWKS.
    Isolated in one place so tests can monkeypatch it with a known key."""
    return _get_jwks_client().get_signing_key_from_jwt(token).key


def verify_token(token: str) -> dict:
    """Verify a Clerk session JWT and return its claims. Raises on any failure."""
    key = _signing_key_for(token)
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        issuer=settings.clerk_issuer or None,     # verify iss when configured
        options={"verify_aud": False, "require": ["exp", "iat"]},
        leeway=5,
    )
    # Clerk stamps 'azp' (authorized party = the frontend origin the token was
    # minted for). When we've been told which origins are ours, reject anything
    # minted for a different origin.
    parties = settings.clerk_authorized_party_list
    if parties:
        azp = claims.get("azp")
        if azp and azp not in parties:
            raise jwt.InvalidTokenError(f"azp {azp!r} not an authorized party")
    return claims


def _name_from(claims: dict) -> Optional[str]:
    if claims.get("name"):
        return claims["name"]
    parts = [claims.get("first_name"), claims.get("last_name")]
    joined = " ".join(p for p in parts if p)
    return joined or None


def _email_from(claims: dict) -> Optional[str]:
    return (
        claims.get("email")
        or claims.get("email_address")
        or claims.get("primary_email")
        or claims.get("primary_email_address")
    )


def _roles_from(claims: dict) -> list:
    raw = claims.get(settings.clerk_role_claim)
    if raw is None:
        raw = claims.get("org_role")          # Clerk organisation role fallback
    if raw is None:
        return []
    return [raw] if isinstance(raw, str) else list(raw)


def _fetch_from_clerk_api(sub: str) -> dict:
    """Look up email/name/role from the Clerk Backend API (cached). Best-effort:
    returns {} on any error so a lookup failure never blocks a valid token."""
    if not (sub and settings.clerk_secret_key):
        return {}
    hit = _api_cache.get(sub)
    now = time.monotonic()
    if hit and now - hit[0] < _API_TTL:
        return hit[1]
    try:
        resp = httpx.get(
            f"https://api.clerk.com/v1/users/{sub}",
            headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        u = resp.json()
        emails = {e.get("id"): e.get("email_address") for e in u.get("email_addresses", [])}
        info = {
            "email": emails.get(u.get("primary_email_address_id"))
            or next(iter(emails.values()), None),
            "name": " ".join(p for p in [u.get("first_name"), u.get("last_name")] if p) or None,
            "role": (u.get("public_metadata") or {}).get("role"),
        }
    except Exception:
        info = {}
    _api_cache[sub] = (now, info)
    return info


def upsert_user_from_clerk(session: Session, claims: dict) -> Optional[User]:
    """Provision/refresh a local user from a verified Clerk token. None if we
    can't determine an email (misconfigured token + no API fallback)."""
    sub = claims.get("sub")
    email = _email_from(claims)
    name = _name_from(claims)
    role_values = _roles_from(claims)

    if (not email or not role_values) and settings.clerk_secret_key:
        info = _fetch_from_clerk_api(sub)
        email = email or info.get("email")
        name = name or info.get("name")
        if not role_values and info.get("role"):
            role_values = [info["role"]]

    if not email:
        return None

    user = None
    if sub:
        user = session.exec(select(User).where(User.clerk_user_id == sub)).first()
    if user is None:
        user = session.exec(select(User).where(User.email == email)).first()

    mapped = map_roles(role_values, settings.clerk_role_map or {}, settings.default_role)
    # Only let the token drive the role when it actually carried role info; a thin
    # token must not silently demote an existing user (mirrors the Entra path).
    has_role = bool(role_values)
    if user is None:
        user = User(email=email, name=name, clerk_user_id=sub, role_code=mapped, active=True)
    else:
        user.clerk_user_id = sub or user.clerk_user_id
        user.name = name or user.name
        if has_role:
            user.role_code = mapped
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
