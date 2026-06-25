"""Phase 2 — Requisitions + tiered approval.

Auth pattern mirrors test_stock.py: override get_current_user with a CurrentUser
for the role under test (the documented way to fake a non-admin user). The DB is
the seeded in-memory SQLite from conftest, so demo SKUs + their BC list prices
are available for estimated-amount maths.

Demo prices used here (from BCAdapter.list_items):
  BOARD-200K = 1.95, INK-FLEXO-CYAN = 14.5
"""
import pytest

from app.auth.deps import CurrentUser, get_current_user
from app.domain.approvals import can_approve, required_tier
from app.gateway.models import OrderEvent
from app.main import app

# Role limits matching db.DEFAULT_ROLES.
LIMITS = {
    "ADMIN": None,
    "APPROVER": 50000.0,
    "OFFICER": 5000.0,
    "REQUESTER": 0.0,
    "VIEWER": 0.0,
}


def as_role(role_code, email=None):
    """Override get_current_user with a synthetic user holding `role_code`."""
    user = CurrentUser(
        id=f"u-{role_code}",
        email=email or f"{role_code.lower()}@golden.com.fj",
        name=role_code.title(),
        role_code=role_code,
        approval_limit=LIMITS[role_code],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.fixture(autouse=True)
def _clear_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _create(client, lines, cost_center="CC-100"):
    return client.post(
        "/api/requisitions",
        json={"cost_center": cost_center, "lines": lines},
    )


def _events_for(engine, req_id):
    from sqlmodel import Session, select
    with Session(engine) as s:
        return s.exec(
            select(OrderEvent)
            .where(OrderEvent.entity_kind == "REQUISITION", OrderEvent.entity_id == req_id)
            .order_by(OrderEvent.id)
        ).all()


# --------------------------------------------------------------------------- #
# Pure helper unit tests (no DB)
# --------------------------------------------------------------------------- #
def test_can_approve_rule():
    # Non-approver roles never approve.
    assert can_approve("REQUESTER", 0.0, 10.0) is False
    assert can_approve("VIEWER", 0.0, 10.0) is False
    assert can_approve(None, None, 10.0) is False
    # OFFICER limited to 5000.
    assert can_approve("OFFICER", 5000.0, 5000.0) is True
    assert can_approve("OFFICER", 5000.0, 5000.01) is False
    # APPROVER limited to 50000.
    assert can_approve("APPROVER", 50000.0, 49999.0) is True
    assert can_approve("APPROVER", 50000.0, 60000.0) is False
    # ADMIN unlimited.
    assert can_approve("ADMIN", None, 10_000_000.0) is True


def test_required_tier_picks_cheapest_covering_role():
    assert required_tier(100.0)["role"] == "OFFICER"
    assert required_tier(5000.0)["role"] == "OFFICER"
    assert required_tier(5000.01)["role"] == "APPROVER"
    assert required_tier(50000.0)["role"] == "APPROVER"
    big = required_tier(80000.0)
    assert big["role"] == "ADMIN"
    assert big["unlimited"] is True


# --------------------------------------------------------------------------- #
# Create / estimated amount
# --------------------------------------------------------------------------- #
def test_create_requisition_is_draft_with_estimate(client):
    as_role("REQUESTER")
    r = _create(client, [{"sku": "BOARD-200K", "quantity": 100}])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "DRAFT"
    assert body["requester"] == "requester@golden.com.fj"
    assert body["number"].startswith("REQ-")
    # 100 * 1.95 = 195
    assert body["estimated_amount"] == pytest.approx(195.0)
    assert body["amount_label"] == "estimated"
    assert body["required_tier"]["role"] == "OFFICER"


def test_viewer_cannot_create(client):
    as_role("VIEWER")
    r = _create(client, [{"sku": "BOARD-200K", "quantity": 1}])
    assert r.status_code == 403


def test_unauthenticated_cannot_create(client):
    # No override -> real get_current_user with no session -> 401.
    app.dependency_overrides.pop(get_current_user, None)
    r = _create(client, [{"sku": "BOARD-200K", "quantity": 1}])
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Submit routes to the right tier by amount
# --------------------------------------------------------------------------- #
def test_small_req_approvable_by_officer(client):
    as_role("REQUESTER")
    # 100 * 1.95 = 195 -> OFFICER tier
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 100}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")

    as_role("OFFICER")
    waiting = client.get("/api/approvals").json()
    assert any(w["id"] == req_id for w in waiting)


def test_large_req_not_in_officer_queue_but_in_approver_queue(client):
    as_role("REQUESTER")
    # 1000 * 14.5 = 14500 -> above OFFICER 5000, within APPROVER 50000
    req_id = _create(client, [{"sku": "INK-FLEXO-CYAN", "quantity": 1000}]).json()["id"]
    body = client.post(f"/api/requisitions/{req_id}/submit").json()
    assert body["status"] == "IN_APPROVAL"
    assert body["required_tier"]["role"] == "APPROVER"

    as_role("OFFICER")
    assert all(w["id"] != req_id for w in client.get("/api/approvals").json())

    as_role("APPROVER")
    assert any(w["id"] == req_id for w in client.get("/api/approvals").json())


# --------------------------------------------------------------------------- #
# Approve / reject
# --------------------------------------------------------------------------- #
def test_approver_with_sufficient_limit_approves(client):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "INK-FLEXO-CYAN", "quantity": 1000}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")

    as_role("APPROVER")
    r = client.post(f"/api/requisitions/{req_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"


def test_approver_with_insufficient_limit_403(client):
    as_role("REQUESTER")
    # 14500 > OFFICER limit 5000
    req_id = _create(client, [{"sku": "INK-FLEXO-CYAN", "quantity": 1000}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")

    as_role("OFFICER")
    r = client.post(f"/api/requisitions/{req_id}/approve")
    assert r.status_code == 403
    # Still IN_APPROVAL — no state change on a forbidden approval.
    as_role("APPROVER")
    assert client.get(f"/api/requisitions/{req_id}").json()["status"] == "IN_APPROVAL"


def test_reject_with_reason(client):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 10}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")

    as_role("APPROVER")
    r = client.post(f"/api/requisitions/{req_id}/reject", json={"reason": "duplicate order"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "REJECTED"
    reject_evt = next(e for e in body["events"] if e["event_type"] == "REJECTED")
    assert reject_evt["detail"]["reason"] == "duplicate order"


# --------------------------------------------------------------------------- #
# Audit trail — every transition writes an order_events row
# --------------------------------------------------------------------------- #
def test_every_transition_is_audited(client, engine):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 10}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")

    as_role("APPROVER")
    client.post(f"/api/requisitions/{req_id}/approve")

    events = _events_for(engine, req_id)
    types = [e.event_type for e in events]
    assert types == ["CREATED", "SUBMITTED", "APPROVED"]
    # from/to statuses captured on each transition.
    assert (events[0].from_status, events[0].to_status) == (None, "DRAFT")
    assert (events[1].from_status, events[1].to_status) == ("DRAFT", "IN_APPROVAL")
    assert (events[2].from_status, events[2].to_status) == ("IN_APPROVAL", "APPROVED")
    # actor recorded.
    assert events[1].actor == "requester@golden.com.fj"
    assert events[2].actor == "approver@golden.com.fj"


def test_cancel_is_audited(client, engine):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 1}]).json()["id"]
    r = client.post(f"/api/requisitions/{req_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    events = _events_for(engine, req_id)
    assert [e.event_type for e in events] == ["CREATED", "CANCELLED"]
    assert events[1].from_status == "DRAFT"
    assert events[1].to_status == "CANCELLED"


# --------------------------------------------------------------------------- #
# Invalid transitions -> 409
# --------------------------------------------------------------------------- #
def test_approve_draft_is_409(client):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 1}]).json()["id"]
    as_role("APPROVER")
    r = client.post(f"/api/requisitions/{req_id}/approve")
    assert r.status_code == 409


def test_double_submit_is_409(client):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 1}]).json()["id"]
    assert client.post(f"/api/requisitions/{req_id}/submit").status_code == 200
    assert client.post(f"/api/requisitions/{req_id}/submit").status_code == 409


def test_edit_after_submit_is_409(client):
    as_role("REQUESTER")
    req_id = _create(client, [{"sku": "BOARD-200K", "quantity": 1}]).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    r = client.put(f"/api/requisitions/{req_id}", json={"cost_center": "CC-999"})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Approvals queue only shows what the caller may approve
# --------------------------------------------------------------------------- #
def test_approvals_queue_filters_by_limit(client):
    as_role("REQUESTER")
    small = _create(client, [{"sku": "BOARD-200K", "quantity": 100}]).json()["id"]   # 195
    large = _create(client, [{"sku": "INK-FLEXO-CYAN", "quantity": 1000}]).json()["id"]  # 14500
    for rid in (small, large):
        client.post(f"/api/requisitions/{rid}/submit")

    as_role("OFFICER")
    ids = {w["id"] for w in client.get("/api/approvals").json()}
    assert small in ids and large not in ids

    as_role("ADMIN", email="admin")
    ids = {w["id"] for w in client.get("/api/approvals").json()}
    assert small in ids and large in ids


def test_list_mine_filter(client):
    as_role("REQUESTER", email="alice@golden.com.fj")
    _create(client, [{"sku": "BOARD-200K", "quantity": 1}])

    as_role("REQUESTER", email="bob@golden.com.fj")
    _create(client, [{"sku": "BOARD-200K", "quantity": 2}])
    mine = client.get("/api/requisitions", params={"mine": "true"}).json()
    assert mine
    assert all(m["requester"] == "bob@golden.com.fj" for m in mine)
