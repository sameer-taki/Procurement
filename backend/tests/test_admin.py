"""Admin panel: user/role management + system health (ADMIN only).

Runs against the conftest-seeded demo DB (seed_roles_and_admin creates the five
roles + the bootstrap admin). Uses the real admin-login session for the ADMIN
path and role overrides for the 403 checks, mirroring test_auth conventions.
"""
import json

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.gateway.models import IntegrationOutbox, OrderEvent, Role, User
from app.main import app


@pytest.fixture(autouse=True)
def _clear_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


def as_role(role_code):
    user = CurrentUser(
        id=f"u-{role_code}", email=f"{role_code.lower()}@golden.com.fj",
        name=role_code.title(), role_code=role_code, approval_limit=None,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _add_user(engine, email, role="VIEWER", active=True):
    with Session(engine) as s:
        u = User(email=email, name=email.split("@")[0], role_code=role, active=active)
        s.add(u)
        s.commit()
        return u.id


# --------------------------------------------------------------------------- #
# Access control: everything under /api/admin is ADMIN-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/admin/users", "/api/admin/roles",
                                  "/api/admin/system"])
def test_admin_endpoints_require_admin(client, path):
    as_role("OFFICER")
    assert client.get(path).status_code == 403


def test_admin_endpoints_require_auth(client):
    assert client.get("/api/admin/users").status_code == 401


# --------------------------------------------------------------------------- #
# Users: list + role/active changes, audited, last-admin guarded
# --------------------------------------------------------------------------- #
def test_list_users_shows_bootstrap_admin(admin_client):
    users = admin_client.get("/api/admin/users").json()
    admins = [u for u in users if u["role"] == "ADMIN"]
    assert admins and admins[0]["active"] is True
    assert {"id", "email", "name", "role", "active", "entra_linked"} <= set(users[0])


def test_promote_user_and_audit(admin_client, engine):
    uid = _add_user(engine, "sameer@golden.com.fj", role="VIEWER")
    out = admin_client.patch(f"/api/admin/users/{uid}", json={"role": "OFFICER"}).json()
    assert out["role"] == "OFFICER"
    with Session(engine) as s:
        ev = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "USER", OrderEvent.entity_id == uid)).all()
        assert len(ev) == 1
        detail = json.loads(ev[0].detail_json)
        assert detail["before"]["role"] == "VIEWER"
        assert detail["after"]["role"] == "OFFICER"


def test_deactivate_and_reactivate_user(admin_client, engine):
    uid = _add_user(engine, "leaver@golden.com.fj", role="OFFICER")
    assert admin_client.patch(f"/api/admin/users/{uid}", json={"active": False}).json()["active"] is False
    assert admin_client.patch(f"/api/admin/users/{uid}", json={"active": True}).json()["active"] is True


def test_unknown_role_is_400(admin_client, engine):
    uid = _add_user(engine, "x@golden.com.fj")
    r = admin_client.patch(f"/api/admin/users/{uid}", json={"role": "SUPERUSER"})
    assert r.status_code == 400


def test_empty_patch_is_400(admin_client, engine):
    uid = _add_user(engine, "y@golden.com.fj")
    assert admin_client.patch(f"/api/admin/users/{uid}", json={}).status_code == 400


def test_last_active_admin_cannot_be_demoted_or_deactivated(admin_client):
    users = admin_client.get("/api/admin/users").json()
    the_admin = next(u for u in users if u["role"] == "ADMIN" and u["active"])
    r = admin_client.patch(f"/api/admin/users/{the_admin['id']}", json={"role": "VIEWER"})
    assert r.status_code == 409
    r = admin_client.patch(f"/api/admin/users/{the_admin['id']}", json={"active": False})
    assert r.status_code == 409


def test_admin_can_be_demoted_once_another_admin_exists(admin_client, engine):
    _add_user(engine, "second-admin@golden.com.fj", role="ADMIN")
    users = admin_client.get("/api/admin/users").json()
    first = next(u for u in users if u["role"] == "ADMIN" and u["email"] != "second-admin@golden.com.fj")
    out = admin_client.patch(f"/api/admin/users/{first['id']}", json={"role": "OFFICER"}).json()
    assert out["role"] == "OFFICER"


# --------------------------------------------------------------------------- #
# Roles: approval limits (what the tiered approval engine routes by)
# --------------------------------------------------------------------------- #
def test_update_role_limit_and_audit(admin_client, engine):
    out = admin_client.patch("/api/admin/roles/OFFICER",
                             json={"approval_limit": 7500}).json()
    assert out["approval_limit"] == 7500
    with Session(engine) as s:
        assert s.get(Role, "OFFICER").approval_limit == 7500
        ev = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "ROLE", OrderEvent.entity_id == "OFFICER")).all()
        assert len(ev) == 1


def test_role_limit_null_means_unlimited(admin_client, engine):
    out = admin_client.patch("/api/admin/roles/APPROVER",
                             json={"approval_limit": None}).json()
    assert out["approval_limit"] is None


def test_role_limit_negative_is_422(admin_client):
    r = admin_client.patch("/api/admin/roles/OFFICER", json={"approval_limit": -1})
    assert r.status_code == 422


def test_unknown_role_is_404(admin_client):
    assert admin_client.patch("/api/admin/roles/NOPE",
                              json={"approval_limit": 1}).status_code == 404


# --------------------------------------------------------------------------- #
# System health + outbox retry
# --------------------------------------------------------------------------- #
def test_system_health_shape(admin_client):
    out = admin_client.get("/api/admin/system").json()
    systems = {s["system"]: s for s in out["integrations"]}
    assert systems["Business Central"]["mode"] == "demo"
    jobs = {j["job"] for j in out["schedulers"]}
    assert jobs == {"stock_refresh", "outbox_process", "usage_import"}
    assert set(out["outbox"]["counts"]) >= {"PENDING", "SENT", "FAILED"}


def _failed_row(engine, entity_ref="po-dead"):
    with Session(engine) as s:
        row = IntegrationOutbox(
            target="BC", action="create_purchase_order", entity_ref=entity_ref,
            request_json=json.dumps({"po_id": entity_ref}), status="FAILED",
            attempts=5, last_error="boom",
        )
        s.add(row)
        s.commit()
        return row.id


def test_failed_rows_surface_in_system_view(admin_client, engine):
    row_id = _failed_row(engine)
    out = admin_client.get("/api/admin/system").json()
    assert out["outbox"]["counts"]["FAILED"] == 1
    assert [r["id"] for r in out["outbox"]["failed_rows"]] == [row_id]


def test_retry_requeues_failed_row(admin_client, engine):
    row_id = _failed_row(engine)
    out = admin_client.post(f"/api/admin/outbox/{row_id}/retry").json()
    assert out["requeued"] == row_id
    with Session(engine) as s:
        row = s.get(IntegrationOutbox, row_id)
        # The unknown po_id fails again during the inline drain, but the retry
        # itself reset attempts and re-entered the pipeline.
        assert row.status in ("PENDING", "FAILED")
        ev = s.exec(select(OrderEvent).where(
            OrderEvent.event_type == "OUTBOX_RETRIED")).all()
        assert len(ev) == 1


def test_retry_non_failed_row_is_409(admin_client, engine):
    with Session(engine) as s:
        row = IntegrationOutbox(target="BC", action="create_purchase_order",
                                entity_ref="po-live", request_json="{}",
                                status="SENT")
        s.add(row)
        s.commit()
        row_id = row.id
    assert admin_client.post(f"/api/admin/outbox/{row_id}/retry").status_code == 409


def test_retry_blocked_by_live_duplicate(admin_client, engine):
    dead_id = _failed_row(engine, entity_ref="po-x")
    with Session(engine) as s:
        s.add(IntegrationOutbox(target="BC", action="create_purchase_order",
                                entity_ref="po-x", request_json="{}",
                                status="PENDING"))
        s.commit()
    r = admin_client.post(f"/api/admin/outbox/{dead_id}/retry")
    assert r.status_code == 409
    assert "already covers" in r.json()["detail"]


def test_retry_unknown_row_is_404(admin_client):
    assert admin_client.post("/api/admin/outbox/999999/retry").status_code == 404
