"""Phase 5 — Analytics (spend / on-time-delivery / stock-turn) + warehouse push.

Figures are computed from canonical data; tests seed receipts via the public
receive endpoint (BOARD-200K @ Pacific, price 1.80, qty 1000) so the spend/OTD
numbers are deterministic. stock_turn is computed over the demo catalog snapshots
seeded by conftest (sum allocated / sum on_hand = 34580 / 89795 = 0.3851).
"""
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.gateway.models import OrderEvent
from app.main import app

LIMITS = {
    "ADMIN": None, "APPROVER": 50000.0, "OFFICER": 5000.0,
    "REQUESTER": 0.0, "VIEWER": 0.0,
}

# Demo stock-turn over the seeded catalog (allocated 34580 / on_hand 89795).
EXPECTED_STOCK_TURN = round(34580 / 89795, 4)


def as_role(role_code, email=None):
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


def _approved_req(client, lines):
    as_role("REQUESTER")
    req_id = client.post(
        "/api/requisitions", json={"cost_center": "CC-100", "lines": lines}
    ).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    as_role("ADMIN", email="admin")
    assert client.post(f"/api/requisitions/{req_id}/approve").json()["status"] == "APPROVED"
    return req_id


def _receive_board(client, qty=1000, needed_by=None):
    """Order + issue + fully receive BOARD-200K (Pacific @1.80). Returns the PO id."""
    line = {"sku": "BOARD-200K", "quantity": qty}
    if needed_by is not None:
        line["needed_by"] = needed_by.isoformat()
    req_id = _approved_req(client, [line])
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    client.post(f"/api/purchase-orders/{po['id']}/issue")
    detail = client.get(f"/api/purchase-orders/{po['id']}").json()
    line_id = detail["lines"][0]["po_line_id"]
    ordered = detail["lines"][0]["quantity"]          # rounded up to MOQ (1000)
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": ordered}]})
    return po["id"], ordered


# --------------------------------------------------------------------------- #
# Spend = sum(received_qty * po_line.unit_price), total + by vendor
# --------------------------------------------------------------------------- #
def test_spend_total_and_by_vendor(client):
    _receive_board(client)                            # 1000 * 1.80 = 1800
    as_role("OFFICER")
    a = client.get("/api/analytics").json()
    assert a["spend"]["total"] == pytest.approx(1800.0)
    by_vendor = {row["vendor"]: row["spend"] for row in a["spend"]["by_vendor"]}
    assert by_vendor["Pacific Paper & Board Ltd"] == pytest.approx(1800.0)


def test_spend_counts_only_received_quantity(client):
    # Order 1000 but receive only 600 -> spend reflects received, not ordered.
    line = {"sku": "BOARD-200K", "quantity": 1000}
    req_id = _approved_req(client, [line])
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    client.post(f"/api/purchase-orders/{po['id']}/issue")
    detail = client.get(f"/api/purchase-orders/{po['id']}").json()
    line_id = detail["lines"][0]["po_line_id"]
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 600}]})
    a = client.get("/api/analytics").json()
    assert a["spend"]["total"] == pytest.approx(600 * 1.80)


def test_spend_zero_with_no_receipts(client):
    as_role("OFFICER")
    a = client.get("/api/analytics").json()
    assert a["spend"]["total"] == pytest.approx(0.0)
    assert a["spend"]["by_vendor"] == []


# --------------------------------------------------------------------------- #
# On-time delivery: % of received lines with a needed_by met
# --------------------------------------------------------------------------- #
def test_on_time_delivery_on_time(client):
    future = date.today() + timedelta(days=30)
    _receive_board(client, needed_by=future)          # received now <= future
    as_role("OFFICER")
    otd = client.get("/api/analytics").json()["on_time_delivery"]
    assert otd["sample"] == 1
    assert otd["on_time"] == 1
    assert otd["rate"] == pytest.approx(1.0)


def test_on_time_delivery_late(client):
    past = date.today() - timedelta(days=5)
    _receive_board(client, needed_by=past)            # received now > past = late
    as_role("OFFICER")
    otd = client.get("/api/analytics").json()["on_time_delivery"]
    assert otd["sample"] == 1
    assert otd["on_time"] == 0
    assert otd["rate"] == pytest.approx(0.0)


def test_on_time_delivery_excludes_lines_without_needed_by(client):
    _receive_board(client)                            # no needed_by on the line
    as_role("OFFICER")
    otd = client.get("/api/analytics").json()["on_time_delivery"]
    assert otd["sample"] == 0
    assert otd["rate"] is None


# --------------------------------------------------------------------------- #
# Stock-turn proxy over the seeded catalog (guarded divide-by-zero, labelled)
# --------------------------------------------------------------------------- #
def test_stock_turn_indicative(client):
    as_role("OFFICER")
    turn = client.get("/api/analytics").json()["stock_turn"]
    assert turn["value"] == pytest.approx(EXPECTED_STOCK_TURN)
    assert "indicative" in turn["note"].lower()


# --------------------------------------------------------------------------- #
# Read is any authed user; as_of present
# --------------------------------------------------------------------------- #
def test_analytics_read_any_authed_user(client):
    as_role("VIEWER")
    r = client.get("/api/analytics")
    assert r.status_code == 200
    assert r.json()["as_of"]


# --------------------------------------------------------------------------- #
# Warehouse push: ADMIN only; demo returns 'skipped:not-configured' (never raises)
# --------------------------------------------------------------------------- #
def test_push_skipped_when_warehouse_unconfigured(client):
    _receive_board(client)
    as_role("ADMIN", email="admin")
    r = client.post("/api/analytics/push")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["warehouse"]) == {"spend", "on_time_delivery", "stock_turn"}
    assert all(v == "skipped:not-configured" for v in body["warehouse"].values())
    assert body["figures"]["spend"]["total"] == pytest.approx(1800.0)


def test_push_requires_admin(client):
    as_role("OFFICER")
    assert client.post("/api/analytics/push").status_code == 403
    as_role("VIEWER")
    assert client.post("/api/analytics/push").status_code == 403


def test_warehouse_writer_no_op_without_raising():
    # Direct unit check of the guarded writer in demo mode.
    from app.gateway import warehouse
    assert warehouse.push("spend", [{"as_of": "x", "spend": 1.0}]) == "skipped:not-configured"
    # Even with no rows it never raises.
    assert warehouse.push("spend", []) == "skipped:not-configured"


# --------------------------------------------------------------------------- #
# Audit sanity: a received PO carries the receiving trail analytics derive from
# --------------------------------------------------------------------------- #
def test_received_po_has_audit_trail(client, engine):
    po_id, _ = _receive_board(client)
    with Session(engine) as s:
        evts = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "PURCHASE_ORDER",
            OrderEvent.entity_id == po_id)).all()
    types = {e.event_type for e in evts}
    assert {"RECEIVED", "RECEIPT_POSTED", "MATCHED"} <= types
