"""Clerk Bearer-token auth path.

Verifies the cloud sign-in flow end to end without touching the network: we mint
an RS256 JWT with a throwaway keypair and monkeypatch the JWKS resolver so
`verify_token` validates against our public key. Covers token acceptance, role
mapping (including the no-escalation guarantee), the thin-token demotion guard,
and rejection of bad tokens.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import clerk
from app.auth.deps import get_current_user  # noqa: F401  (imported for symmetry)

ISSUER = "https://clerk.test"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def clerk_env(monkeypatch, rsa_key):
    """Configure Clerk on the singleton settings and resolve tokens to our key."""
    monkeypatch.setattr(clerk.settings, "clerk_issuer", ISSUER, raising=False)
    monkeypatch.setattr(clerk, "_signing_key_for", lambda token: rsa_key.public_key())
    return rsa_key


def make_token(rsa_key, **claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "iat": now, "exp": now + 3600, "azp": "https://app.test"}
    payload.update(claims)
    return jwt.encode(payload, rsa_key, algorithm="RS256")


def auth_get(client, path, token):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_bearer_token_provisions_user_and_maps_role(client, clerk_env):
    token = make_token(
        clerk_env, sub="user_abc", email="jane@golden.com.fj", name="Jane", role="OFFICER"
    )
    me = auth_get(client, "/api/me", token)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == "jane@golden.com.fj"
    assert body["role"] == "OFFICER"
    assert body["can_mutate"] is True


def test_bearer_token_role_does_not_escalate_on_substring(client, clerk_env):
    token = make_token(
        clerk_env, sub="user_x", email="x@golden.com.fj", role="Finance-Admins"
    )
    body = auth_get(client, "/api/me", token).json()
    assert body["role"] == "VIEWER"          # default; 'Finance-Admins' never => ADMIN


def test_bearer_token_role_map(client, clerk_env, monkeypatch):
    monkeypatch.setattr(
        clerk.settings, "clerk_role_map", {"proc-approvers": "APPROVER"}, raising=False
    )
    token = make_token(
        clerk_env, sub="u2", email="a@golden.com.fj", role="proc-approvers"
    )
    assert auth_get(client, "/api/me", token).json()["role"] == "APPROVER"


def test_thin_token_does_not_demote_existing_user(client, clerk_env):
    # First login carries a role → OFFICER.
    t1 = make_token(clerk_env, sub="u3", email="b@golden.com.fj", role="OFFICER")
    assert auth_get(client, "/api/me", t1).json()["role"] == "OFFICER"
    # A later token with NO role claim must preserve the existing role, not demote.
    t2 = make_token(clerk_env, sub="u3", email="b@golden.com.fj")
    assert auth_get(client, "/api/me", t2).json()["role"] == "OFFICER"


def test_expired_token_rejected(client, clerk_env):
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "iat": now - 7200, "exp": now - 3600, "sub": "u4",
         "email": "c@golden.com.fj"},
        clerk_env, algorithm="RS256",
    )
    assert auth_get(client, "/api/me", token).status_code == 401


def test_wrong_issuer_rejected(client, clerk_env):
    now = int(time.time())
    token = jwt.encode(
        {"iss": "https://evil.test", "iat": now, "exp": now + 3600, "sub": "u5",
         "email": "d@golden.com.fj"},
        clerk_env, algorithm="RS256",
    )
    assert auth_get(client, "/api/me", token).status_code == 401


def test_providers_reports_clerk(client, clerk_env):
    assert client.get("/auth/providers").json()["clerk"] is True


def test_session_cookie_still_works_when_clerk_enabled(admin_client, clerk_env):
    # Break-glass admin (session cookie) must keep working alongside Clerk.
    me = admin_client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["role"] == "ADMIN"
