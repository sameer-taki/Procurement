"""Phase 3 — PO posting + vendor email.

Mirrors test_requisitions.py: override get_current_user with a synthetic
CurrentUser per role under test. The seeded in-memory SQLite from conftest gives
us the demo catalog AND the demo vendors/vendor_prices (refresh_all -> seed_vendors).

Demo vendor prices used here (from fakes.VENDOR_PRICES):
  BOARD-200K: Pacific 1.80 (moq 1000), Fiji 1.88 (moq 500)  -> cheapest Pacific
  INK-FLEXO-CYAN: Pacific 13.50 (lt 30, moq 20), Fiji 13.50 (lt 20, moq 10)
                  -> tie on price, Fiji wins on lower lead time
"""
import json

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.domain import purchasing
from app.gateway import bc as bc_module
from app.gateway.models import (
    ExternalRef,
    IntegrationOutbox,
    OrderEvent,
    PurchaseOrder,
    Requisition,
    Vendor,
    VendorPrice,
)
from app.main import app

LIMITS = {
    "ADMIN": None,
    "APPROVER": 50000.0,
    "OFFICER": 5000.0,
    "REQUESTER": 0.0,
    "VIEWER": 0.0,
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
# Build an APPROVED requisition via the public API.
# --------------------------------------------------------------------------- #
def _approved_req(client, lines):
    as_role("REQUESTER")
    req_id = client.post(
        "/api/requisitions", json={"cost_center": "CC-100", "lines": lines}
    ).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    as_role("ADMIN", email="admin")          # ADMIN approves any amount
    r = client.post(f"/api/requisitions/{req_id}/approve")
    assert r.json()["status"] == "APPROVED", r.text
    return req_id


def _po_events(engine, po_id):
    with Session(engine) as s:
        return s.exec(
            select(OrderEvent)
            .where(OrderEvent.entity_kind == "PURCHASE_ORDER",
                   OrderEvent.entity_id == po_id)
            .order_by(OrderEvent.id)
        ).all()


# --------------------------------------------------------------------------- #
# Vendor selection (cheapest + MOQ rounding + tie-break)
# --------------------------------------------------------------------------- #
def test_vendor_selection_cheapest_and_moq(client, engine):
    # Order 100 BOARD-200K: cheapest is Pacific @1.80, but moq 1000 -> qty 1000.
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 100}])
    as_role("OFFICER")
    pos = client.post(f"/api/requisitions/{req_id}/create-po").json()
    assert len(pos) == 1
    po = pos[0]
    assert po["vendor"]["name"] == "Pacific Paper & Board Ltd"
    line = po["lines"][0]
    assert line["sku"] == "BOARD-200K"
    assert line["unit_price"] == pytest.approx(1.80)
    assert line["quantity"] == pytest.approx(1000)        # rounded up to MOQ
    assert po["total"] == pytest.approx(1000 * 1.80)


def test_vendor_selection_tie_break_on_lead_time(client):
    # INK-FLEXO-CYAN: both vendors 13.50; Fiji has lower lead time -> Fiji wins.
    req_id = _approved_req(client, [{"sku": "INK-FLEXO-CYAN", "quantity": 5}])
    as_role("OFFICER")
    pos = client.post(f"/api/requisitions/{req_id}/create-po").json()
    assert len(pos) == 1
    assert pos[0]["vendor"]["name"] == "Fiji Industrial Supplies"
    # moq 10 from Fiji -> qty rounds up to 10.
    assert pos[0]["lines"][0]["quantity"] == pytest.approx(10)


def test_lines_grouped_into_one_po_per_vendor(client):
    # BOARD-200K -> Pacific ; GLUE-STARCH -> Fiji  => two POs.
    req_id = _approved_req(client, [
        {"sku": "BOARD-200K", "quantity": 2000},
        {"sku": "GLUE-STARCH", "quantity": 500},
    ])
    as_role("OFFICER")
    pos = client.post(f"/api/requisitions/{req_id}/create-po").json()
    vendors = sorted(p["vendor"]["name"] for p in pos)
    assert vendors == ["Fiji Industrial Supplies", "Pacific Paper & Board Ltd"]


# --------------------------------------------------------------------------- #
# create-po: only from APPROVED, closes the req, idempotent
# --------------------------------------------------------------------------- #
def test_create_po_only_from_approved(client):
    as_role("REQUESTER")
    req_id = client.post(
        "/api/requisitions", json={"cost_center": "CC", "lines": [
            {"sku": "BOARD-200K", "quantity": 1000}]}
    ).json()["id"]   # DRAFT, not approved
    as_role("OFFICER")
    r = client.post(f"/api/requisitions/{req_id}/create-po")
    assert r.status_code == 409


def test_create_po_closes_requisition(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    client.post(f"/api/requisitions/{req_id}/create-po")
    as_role("ADMIN", email="admin")
    assert client.get(f"/api/requisitions/{req_id}").json()["status"] == "CLOSED"


def test_create_po_is_idempotent(client, engine):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    first = client.post(f"/api/requisitions/{req_id}/create-po").json()
    second = client.post(f"/api/requisitions/{req_id}/create-po").json()
    assert {p["id"] for p in first} == {p["id"] for p in second}
    with Session(engine) as s:
        pos = s.exec(select(PurchaseOrder).where(
            PurchaseOrder.requisition_id == req_id)).all()
    assert len(pos) == 1


def test_po_created_event_on_requisition(client, engine):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    client.post(f"/api/requisitions/{req_id}/create-po")
    with Session(engine) as s:
        evts = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "REQUISITION",
            OrderEvent.entity_id == req_id,
            OrderEvent.event_type == "PO_CREATED")).all()
    assert len(evts) == 1
    assert evts[0].to_status == "CLOSED"


# --------------------------------------------------------------------------- #
# issue: enqueues exactly one outbox row, moves DRAFT -> PO_ISSUED
# --------------------------------------------------------------------------- #
def _create_one_po(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    return client.post(f"/api/requisitions/{req_id}/create-po").json()[0]["id"]


def test_issue_enqueues_one_outbox_row(client, engine):
    po_id = _create_one_po(client)
    # Disable BC processing-on-issue effect by checking outbox before & after?
    # issue both enqueues AND processes; assert exactly one outbox row exists.
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po_id}/issue").json()
    # In demo mode the immediate process_outbox posts to the fake BC, so the PO
    # lands ACKNOWLEDGED; the outbox row is SENT (still exactly one row).
    assert body["status"] in ("PO_ISSUED", "ACKNOWLEDGED")
    with Session(engine) as s:
        rows = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "create_purchase_order")).all()
        relevant = [r for r in rows if json.loads(r.request_json)["po_id"] == po_id]
    assert len(relevant) == 1


def test_issue_records_po_issued_event(client, engine):
    po_id = _create_one_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po_id}/issue")
    types = [e.event_type for e in _po_events(engine, po_id)]
    assert "PO_CREATED" in types and "PO_ISSUED" in types


def test_issue_non_draft_is_409(client):
    po_id = _create_one_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po_id}/issue")        # -> ACKNOWLEDGED in demo
    r = client.post(f"/api/purchase-orders/{po_id}/issue")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# process_outbox: posts to (fake) BC -> ExternalRef + ACKNOWLEDGED + email status
# --------------------------------------------------------------------------- #
def test_process_outbox_posts_and_acknowledges(client, engine):
    po_id = _create_one_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po_id}/issue")        # enqueue + process

    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        assert po.status == "ACKNOWLEDGED"
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "PO",
            ExternalRef.entity_id == po_id,
            ExternalRef.system == "BC")).all()
        assert len(refs) == 1
        assert refs[0].external_id.startswith("BCPO-")
        outbox = s.exec(select(IntegrationOutbox)).all()
        assert all(r.status == "SENT" for r in outbox if
                   json.loads(r.request_json)["po_id"] == po_id)

    detail = client.get(f"/api/purchase-orders/{po_id}").json()
    assert detail["bc_po_no"].startswith("BCPO-")
    assert detail["email_status"] == "skipped:not-configured"


def test_email_skipped_when_graph_unconfigured(client, engine):
    po_id = _create_one_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po_id}/issue")
    evts = [e for e in _po_events(engine, po_id)
            if e.event_type == "VENDOR_NOTIFIED"]
    assert len(evts) == 1
    detail = json.loads(evts[0].detail_json)
    assert detail["email_status"] == "skipped:not-configured"


# --------------------------------------------------------------------------- #
# NEVER DOUBLE-POST: running process_outbox twice -> one BC post / one ExternalRef
# --------------------------------------------------------------------------- #
def test_process_outbox_twice_never_double_posts(client, engine, monkeypatch):
    po_id = _create_one_po(client)

    calls = {"n": 0}
    real = bc_module.BCAdapter.create_purchase_order

    def _counting(self, payload):
        calls["n"] += 1
        return real(self, payload)

    monkeypatch.setattr(bc_module.BCAdapter, "create_purchase_order", _counting)

    # Enqueue once (do NOT process via the issue endpoint to control the count).
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        s.commit()
        purchasing.enqueue_po(s, po)

    with Session(engine) as s:
        purchasing.process_outbox(s)
    with Session(engine) as s:
        purchasing.process_outbox(s)        # second run must be a no-op post-wise

    assert calls["n"] == 1                  # exactly ONE BC post
    with Session(engine) as s:
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "PO",
            ExternalRef.entity_id == po_id,
            ExternalRef.system == "BC")).all()
    assert len(refs) == 1                   # exactly ONE ExternalRef


# --------------------------------------------------------------------------- #
# Retry: a failing BC post increments attempts/last_error, stays PENDING, no ref;
# a later successful run completes it.
# --------------------------------------------------------------------------- #
def test_failing_bc_post_retries_then_succeeds(client, engine, monkeypatch):
    po_id = _create_one_po(client)
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        s.commit()
        purchasing.enqueue_po(s, po)

    def _boom(self, payload):
        raise RuntimeError("BC unreachable")

    monkeypatch.setattr(bc_module.BCAdapter, "create_purchase_order", _boom)
    with Session(engine) as s:
        purchasing.process_outbox(s)

    with Session(engine) as s:
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "create_purchase_order")).first()
        assert row.status == "PENDING"
        assert row.attempts == 1
        assert "BC unreachable" in (row.last_error or "")
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_id == po_id)).all()
        assert refs == []                   # no crosswalk written on failure
        po = s.get(PurchaseOrder, po_id)
        assert po.status == "PO_ISSUED"     # not acknowledged

    # Recover: real adapter succeeds on the retry.
    monkeypatch.undo()
    with Session(engine) as s:
        purchasing.process_outbox(s)
    with Session(engine) as s:
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "create_purchase_order")).first()
        assert row.status == "SENT"
        po = s.get(PurchaseOrder, po_id)
        assert po.status == "ACKNOWLEDGED"
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_id == po_id, ExternalRef.system == "BC")).all()
        assert len(refs) == 1


# --------------------------------------------------------------------------- #
# Hardening: exhausted retries -> terminal FAILED + audit (no silent zombie row)
# --------------------------------------------------------------------------- #
def test_exhausted_bc_post_marked_failed(client, engine, monkeypatch):
    po_id = _create_one_po(client)
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        s.commit()
        purchasing.enqueue_po(s, po)

    def _boom(self, payload):
        raise RuntimeError("BC down")

    monkeypatch.setattr(bc_module.BCAdapter, "create_purchase_order", _boom)
    # 5 attempts (MAX_ATTEMPTS) -> the 5th flips PENDING to terminal FAILED.
    for _ in range(purchasing.MAX_ATTEMPTS):
        with Session(engine) as s:
            purchasing.process_outbox(s)

    with Session(engine) as s:
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.entity_ref == po_id)).first()
        assert row.status == "FAILED"
        assert row.attempts == purchasing.MAX_ATTEMPTS
        evts = [e.event_type for e in _po_events(engine, po_id)]
        assert "BC_POST_FAILED" in evts


def test_failed_row_does_not_block_fresh_enqueue(client, engine, monkeypatch):
    # A genuinely dead (FAILED) row must not block a later fresh attempt for the PO.
    po_id = _create_one_po(client)
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        s.commit()
        purchasing.enqueue_po(s, po)
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.entity_ref == po_id)).first()
        row.status = "FAILED"
        s.add(row)
        s.commit()

    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        fresh = purchasing.enqueue_po(s, po)        # must create a NEW live row
        assert fresh.status == "PENDING"
    with Session(engine) as s:
        live = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.entity_ref == po_id,
            IntegrationOutbox.status != "FAILED")).all()
        assert len(live) == 1


# --------------------------------------------------------------------------- #
# Hardening: no-vendor-price req is NOT closed with zero POs (HIGH finding)
# --------------------------------------------------------------------------- #
def test_unpriced_requisition_stays_approved_no_event(client, engine):
    # BOX-RSC-A is a FINISHED item with no vendor_price.
    req_id = _approved_req(client, [{"sku": "BOX-RSC-A", "quantity": 10}])
    as_role("OFFICER")
    r = client.post(f"/api/requisitions/{req_id}/create-po")
    assert r.status_code == 400
    with Session(engine) as s:
        req = s.get(Requisition, req_id)
        assert req.status == "APPROVED"        # NOT closed
        pos = s.exec(select(PurchaseOrder).where(
            PurchaseOrder.requisition_id == req_id)).all()
        assert pos == []
        evts = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "REQUISITION",
            OrderEvent.entity_id == req_id,
            OrderEvent.event_type == "PO_CREATED")).all()
        assert evts == []                       # no misleading audit row


def test_partial_priced_requisition_records_skipped(client, engine):
    # One priced line (BOARD-200K -> Pacific) + one unpriced (BOX-RSC-A).
    req_id = _approved_req(client, [
        {"sku": "BOARD-200K", "quantity": 1000},
        {"sku": "BOX-RSC-A", "quantity": 10},
    ])
    as_role("OFFICER")
    pos = client.post(f"/api/requisitions/{req_id}/create-po").json()
    assert len(pos) == 1
    with Session(engine) as s:
        evt = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "REQUISITION",
            OrderEvent.entity_id == req_id,
            OrderEvent.event_type == "PO_CREATED")).first()
        detail = json.loads(evt.detail_json)
        assert detail["skipped_skus"] == ["BOX-RSC-A"]


# --------------------------------------------------------------------------- #
# Hardening: duplicate crosswalk insert is treated as 'already posted' (no
# double-post), even when the idempotency read-guard is bypassed.
# --------------------------------------------------------------------------- #
def test_duplicate_crosswalk_does_not_double_post(client, engine, monkeypatch):
    po_id = _create_one_po(client)
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        # Simulate a racing worker that already wrote the crosswalk + a fresh
        # PENDING outbox row. The _bc_ref read-guard would normally catch this; to
        # prove the DB constraint also catches it, monkeypatch _bc_ref to lie.
        s.add(ExternalRef(
            entity_kind="PO", entity_id=po_id, system="BC",
            external_type="PURCHASE_ORDER", external_id="BCPO-RACE",
            external_status="POSTED",
        ))
        s.commit()
        purchasing.enqueue_po(s, po)

    monkeypatch.setattr(purchasing, "_bc_ref", lambda session, pid: None)
    calls = {"n": 0}
    real = bc_module.BCAdapter.create_purchase_order

    def _counting(self, payload):
        calls["n"] += 1
        return real(self, payload)

    monkeypatch.setattr(bc_module.BCAdapter, "create_purchase_order", _counting)
    with Session(engine) as s:
        result = purchasing.process_outbox(s)

    # BC may be called once (the read-guard was disabled) but the duplicate
    # crosswalk insert must be rejected -> still exactly one ExternalRef.
    with Session(engine) as s:
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "PO",
            ExternalRef.entity_id == po_id,
            ExternalRef.system == "BC")).all()
    assert len(refs) == 1
    assert result["skipped"] >= 1


# --------------------------------------------------------------------------- #
# Hardening: vendor email body is HTML-escaped (SECURITY lens)
# --------------------------------------------------------------------------- #
def test_vendor_email_html_is_escaped(client, engine, monkeypatch):
    import app.mailer as mailer
    po_id = _create_one_po(client)
    # Inject markup into the vendor name (originates from BC master in prod).
    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        vendor = s.get(Vendor, po.vendor_id)
        vendor.name = "<script>alert(1)</script>"
        vendor.email = "sales@pacific.com.fj"
        s.add(vendor)
        s.commit()

    # Enable Graph and capture the HTML body without sending.
    monkeypatch.setattr(mailer.settings, "graph_tenant_id", "t", raising=False)
    monkeypatch.setattr(mailer.settings, "graph_client_id", "c", raising=False)
    monkeypatch.setattr(mailer.settings, "graph_client_secret", "s", raising=False)
    captured = {}
    monkeypatch.setattr(mailer, "send_mail",
                        lambda to, subject, html: captured.update(html=html))

    with Session(engine) as s:
        po = s.get(PurchaseOrder, po_id)
        po.status = "PO_ISSUED"
        s.add(po)
        s.commit()
        purchasing.enqueue_po(s, po)
    with Session(engine) as s:
        purchasing.process_outbox(s)

    assert "<script>" not in captured["html"]
    assert "&lt;script&gt;" in captured["html"]


# --------------------------------------------------------------------------- #
# RBAC + listing + vendors
# --------------------------------------------------------------------------- #
def test_viewer_cannot_create_po(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("VIEWER")
    assert client.post(f"/api/requisitions/{req_id}/create-po").status_code == 403


def test_viewer_cannot_issue(client):
    po_id = _create_one_po(client)
    as_role("VIEWER")
    assert client.post(f"/api/purchase-orders/{po_id}/issue").status_code == 403


def test_outbox_process_endpoint_admin_only(client):
    as_role("OFFICER")
    assert client.post("/api/outbox/process").status_code == 403
    as_role("ADMIN", email="admin")
    assert client.post("/api/outbox/process").status_code == 200


def test_list_purchase_orders_and_filter(client):
    po_id = _create_one_po(client)
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po_id}/issue")
    all_pos = client.get("/api/purchase-orders").json()
    assert any(p["id"] == po_id for p in all_pos)
    row = next(p for p in all_pos if p["id"] == po_id)
    assert row["requisition_number"].startswith("REQ-")
    assert row["bc_po_no"].startswith("BCPO-")
    ack = client.get("/api/purchase-orders", params={"status": "ACKNOWLEDGED"}).json()
    assert any(p["id"] == po_id for p in ack)


def test_list_vendors(client):
    as_role("OFFICER")
    vendors = client.get("/api/vendors").json()
    names = {v["name"] for v in vendors}
    assert "Pacific Paper & Board Ltd" in names
    assert "Fiji Industrial Supplies" in names


def test_vendors_seeded_with_prices(client, engine):
    with Session(engine) as s:
        assert s.exec(select(Vendor)).all()
        assert s.exec(select(VendorPrice)).all()
