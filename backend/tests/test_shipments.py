"""Shipment tracking (SOP step 8) — the in-transit record per PO.

Reuses the test_receiving pattern: build an APPROVED req -> create-po -> issue
(posts to fake BC, PO lands ACKNOWLEDGED), then record shipments against it.
Statuses: CONFIRMED | ON_WATER | ARRIVED | RECEIVED | CANCELLED; open =
CONFIRMED/ON_WATER. The Shipping Schedule lists open shipments first by ETA, and
the Order Page's next_eta is the earliest OPEN shipment ETA for an item on that
PO (an ARRIVED shipment is no longer 'next').

The Order Page tie-in uses a paper PO (BX200-1950 x 25000 = 1 FCL from Changle
Numat (CSC)); shipment CRUD itself runs on the cheap BOARD-200K PO the other
suites use. Every create/update lands an OrderEvent on the PO
(SHIPMENT_RECORDED / SHIPMENT_UPDATED, entity_kind PURCHASE_ORDER).
"""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.gateway.models import OrderEvent
from app.main import app

LIMITS = {
    "ADMIN": None, "APPROVER": 50000.0, "OFFICER": 5000.0,
    "REQUESTER": 0.0, "VIEWER": 0.0,
}


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


# --------------------------------------------------------------------------- #
# Build an ISSUED PO (posted to fake BC) we can record shipments against.
# --------------------------------------------------------------------------- #
def _approved_req(client, lines):
    as_role("REQUESTER")
    req_id = client.post(
        "/api/requisitions", json={"cost_center": "CC-100", "lines": lines}
    ).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    as_role("ADMIN", email="admin")
    r = client.post(f"/api/requisitions/{req_id}/approve")
    assert r.json()["status"] == "APPROVED", r.text
    return req_id


def _issued_po(client, lines=None):
    lines = lines or [{"sku": "BOARD-200K", "quantity": 1000}]
    req_id = _approved_req(client, lines)
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    issued = client.post(f"/api/purchase-orders/{po['id']}/issue").json()
    assert issued["status"] == "ACKNOWLEDGED", issued        # posted to fake BC
    return client.get(f"/api/purchase-orders/{po['id']}").json()


def _draft_po(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    return client.post(f"/api/requisitions/{req_id}/create-po").json()[0]


def _po_events(engine, po_id, event_type=None):
    with Session(engine) as s:
        rows = s.exec(
            select(OrderEvent)
            .where(OrderEvent.entity_kind == "PURCHASE_ORDER",
                   OrderEvent.entity_id == po_id)
            .order_by(OrderEvent.id)
        ).all()
    if event_type:
        rows = [e for e in rows if e.event_type == event_type]
    return rows


def _iso(days_from_now):
    return (date.today() + timedelta(days=days_from_now)).isoformat()


# --------------------------------------------------------------------------- #
# Create: 201 on an issued PO with the recorded fields; status defaults CONFIRMED
# --------------------------------------------------------------------------- #
def test_create_shipment_on_issued_po(client):
    po = _issued_po(client)
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/shipments", json={
        "vessel": "Capitaine Tasman", "etd": _iso(2), "eta": _iso(30),
        "rolls": 44, "weight_kg": 25000, "fcl_count": 1, "notes": "1 x 40ft FCL",
    })
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["id"]
    assert s["po_id"] == po["id"]
    assert s["vessel"] == "Capitaine Tasman"
    assert s["etd"] == _iso(2)
    assert s["eta"] == _iso(30)
    assert s["rolls"] == 44
    assert s["weight_kg"] == pytest.approx(25000)
    assert s["fcl_count"] == 1
    assert s["notes"] == "1 x 40ft FCL"
    assert s["status"] == "CONFIRMED"                        # default when omitted
    assert s["created_at"] and s["updated_at"]


def test_create_shipment_explicit_status(client):
    po = _issued_po(client)
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                    json={"status": "ON_WATER", "eta": _iso(10)})
    assert r.status_code == 201
    assert r.json()["status"] == "ON_WATER"


def test_create_shipment_on_draft_po_is_409(client):
    po = _draft_po(client)                                   # created, not issued
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/shipments", json={})
    assert r.status_code == 409


def test_create_shipment_unknown_po_is_404(client):
    as_role("OFFICER")
    assert client.post("/api/purchase-orders/nope/shipments",
                       json={}).status_code == 404


def test_create_shipment_bad_status_is_400(client):
    po = _issued_po(client)
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                    json={"status": "SUNK"})
    assert r.status_code == 400


def test_create_shipment_negative_counts_are_422(client):
    po = _issued_po(client)
    as_role("OFFICER")
    for bad in ({"rolls": -1}, {"weight_kg": -5}, {"fcl_count": -2}):
        r = client.post(f"/api/purchase-orders/{po['id']}/shipments", json=bad)
        assert r.status_code == 422, bad


# --------------------------------------------------------------------------- #
# PATCH: partial update touches only the sent fields and bumps updated_at
# --------------------------------------------------------------------------- #
def test_patch_partial_update(client):
    po = _issued_po(client)
    as_role("OFFICER")
    created = client.post(f"/api/purchase-orders/{po['id']}/shipments", json={
        "vessel": "Capitaine Tasman", "eta": _iso(30), "rolls": 44,
    }).json()

    r = client.patch(f"/api/shipments/{created['id']}",
                     json={"eta": _iso(25), "status": "ON_WATER"})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["eta"] == _iso(25)                        # changed
    assert updated["status"] == "ON_WATER"                   # changed
    assert updated["vessel"] == "Capitaine Tasman"           # untouched
    assert updated["rolls"] == 44                            # untouched
    assert updated["etd"] is None                            # untouched
    assert (datetime.fromisoformat(updated["updated_at"])
            > datetime.fromisoformat(created["updated_at"]))


def test_patch_empty_body_is_400(client):
    po = _issued_po(client)
    as_role("OFFICER")
    sid = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                      json={}).json()["id"]
    assert client.patch(f"/api/shipments/{sid}", json={}).status_code == 400


def test_patch_bad_status_is_400(client):
    po = _issued_po(client)
    as_role("OFFICER")
    sid = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                      json={}).json()["id"]
    r = client.patch(f"/api/shipments/{sid}", json={"status": "SUNK"})
    assert r.status_code == 400


def test_patch_unknown_shipment_is_404(client):
    as_role("OFFICER")
    assert client.patch("/api/shipments/nope",
                        json={"vessel": "X"}).status_code == 404


# --------------------------------------------------------------------------- #
# Shipping Schedule: open (CONFIRMED/ON_WATER) first by ETA; PO context attached
# --------------------------------------------------------------------------- #
def test_shipping_schedule_open_by_eta_first(client):
    po = _issued_po(client)
    as_role("OFFICER")

    def _mk(**body):
        return client.post(f"/api/purchase-orders/{po['id']}/shipments",
                           json=body).json()["id"]

    arrived = _mk(status="ARRIVED", eta=_iso(5))             # earliest ETA, closed
    confirmed_late = _mk(status="CONFIRMED", eta=_iso(30))
    onwater_soon = _mk(status="ON_WATER", eta=_iso(10))
    confirmed_no_eta = _mk(status="CONFIRMED")               # open, no ETA yet

    rows = client.get("/api/shipments").json()
    # Open by ETA (10 then 30), then open-without-ETA, then non-open — the ARRIVED
    # shipment sorts last despite having the earliest ETA.
    assert [r["id"] for r in rows] == [
        onwater_soon, confirmed_late, confirmed_no_eta, arrived,
    ]
    first = rows[0]
    assert first["po_number"] == po["number"]
    assert first["po_status"] == "ACKNOWLEDGED"
    assert first["vendor"] == "Pacific Paper & Board Ltd"


def test_shipping_schedule_status_filter(client):
    po = _issued_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po['id']}/shipments",
                json={"status": "CONFIRMED"})
    onwater = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                          json={"status": "ON_WATER"}).json()["id"]
    rows = client.get("/api/shipments", params={"status": "ON_WATER"}).json()
    assert [r["id"] for r in rows] == [onwater]


def test_shipping_schedule_bad_filter_is_400(client):
    as_role("VIEWER")
    assert client.get("/api/shipments",
                      params={"status": "SUNK"}).status_code == 400


def test_shipping_schedule_requires_auth(client):
    assert client.get("/api/shipments").status_code == 401


# --------------------------------------------------------------------------- #
# PO detail carries its shipments (without the schedule's PO context keys)
# --------------------------------------------------------------------------- #
def test_po_detail_includes_shipments(client):
    po = _issued_po(client)
    as_role("OFFICER")
    sid = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                      json={"vessel": "Capitaine Tasman"}).json()["id"]
    detail = client.get(f"/api/purchase-orders/{po['id']}").json()
    assert len(detail["shipments"]) == 1
    shipment = detail["shipments"][0]
    assert shipment["id"] == sid
    assert shipment["vessel"] == "Capitaine Tasman"
    # Nested under its own PO, the schedule-only context keys are absent.
    assert "po_number" not in shipment
    assert "vendor" not in shipment


# --------------------------------------------------------------------------- #
# Audit: SHIPMENT_RECORDED / SHIPMENT_UPDATED land on the PO timeline
# --------------------------------------------------------------------------- #
def test_shipment_events_on_po_timeline(client, engine):
    po = _issued_po(client)
    as_role("OFFICER")
    sid = client.post(f"/api/purchase-orders/{po['id']}/shipments", json={
        "vessel": "Capitaine Tasman", "eta": _iso(30)}).json()["id"]
    client.patch(f"/api/shipments/{sid}", json={"status": "ON_WATER"})

    recorded = _po_events(engine, po["id"], "SHIPMENT_RECORDED")
    assert len(recorded) == 1
    assert recorded[0].actor == "officer@golden.com.fj"
    detail = json.loads(recorded[0].detail_json)
    assert detail["shipment_id"] == sid
    assert detail["vessel"] == "Capitaine Tasman"

    updated = _po_events(engine, po["id"], "SHIPMENT_UPDATED")
    assert len(updated) == 1
    changes = json.loads(updated[0].detail_json)["changes"]
    assert changes == {"status": "ON_WATER"}

    # Visible on the PO timeline the UI renders (order -> vessel -> receipt).
    types = [e["event_type"]
             for e in client.get(f"/api/purchase-orders/{po['id']}").json()["events"]]
    assert "SHIPMENT_RECORDED" in types
    assert "SHIPMENT_UPDATED" in types


# --------------------------------------------------------------------------- #
# Order Page tie-in: next_eta is the earliest OPEN shipment ETA for the item
# --------------------------------------------------------------------------- #
def test_order_page_next_eta_from_earliest_open_shipment(client):
    # 1 FCL of BX200-1950 (Changle Numat (CSC), moq 25000) on the water.
    po = _issued_po(client, [{"sku": "BX200-1950", "quantity": 25000}])
    as_role("OFFICER")

    def _mk(**body):
        r = client.post(f"/api/purchase-orders/{po['id']}/shipments", json=body)
        assert r.status_code == 201, r.text

    _mk(status="ARRIVED", eta=_iso(5))                       # closed: ignored
    _mk(status="CONFIRMED", eta=_iso(40))
    _mk(status="ON_WATER", eta=_iso(20))                     # earliest OPEN

    page = client.get("/api/planning/order-page").json()
    rows = {r["sku"]: r for r in page["rows"]}
    assert rows["BX200-1950"]["next_eta"] == _iso(20)
    # A grade with no open shipment has no next arrival to show.
    assert rows["CWT140-1400"]["next_eta"] is None


# --------------------------------------------------------------------------- #
# RBAC: a VIEWER can read the schedule but never writes
# --------------------------------------------------------------------------- #
def test_viewer_can_read_schedule(client):
    as_role("VIEWER")
    assert client.get("/api/shipments").status_code == 200


def test_viewer_cannot_create_shipment(client):
    po = _issued_po(client)
    as_role("VIEWER")
    r = client.post(f"/api/purchase-orders/{po['id']}/shipments", json={})
    assert r.status_code == 403


def test_viewer_cannot_patch_shipment(client):
    po = _issued_po(client)
    as_role("OFFICER")
    sid = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                      json={}).json()["id"]
    as_role("VIEWER")
    assert client.patch(f"/api/shipments/{sid}",
                        json={"vessel": "X"}).status_code == 403


def test_patch_explicit_null_status_is_400(client):
    """An explicit "status": null must be a client error, not a database
    NOT NULL constraint blowing up as a 500."""
    po = _issued_po(client)
    as_role("OFFICER")
    ship = client.post(f"/api/purchase-orders/{po['id']}/shipments",
                       json={"vessel": "Null Test"}).json()
    r = client.patch(f"/api/shipments/{ship['id']}", json={"status": None})
    assert r.status_code == 400
    assert "null" in r.json()["detail"]
