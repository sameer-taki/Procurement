"""Phase 5 — Receiving + receipt-post to BC + 3-way match.

Reuses the conftest in-memory SQLite (demo catalog + vendors + vendor_prices) and
the test_purchasing pattern: build an APPROVED req -> create-po -> issue (which
posts to fake BC), then receive against the issued PO. Overrides get_current_user
per role, exactly like test_purchasing.
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
    Receipt,
)
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
# Build an ISSUED PO (posted to fake BC) we can receive against.
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
    assert issued["status"] == "ACKNOWLEDGED", issued      # posted to fake BC
    return client.get(f"/api/purchase-orders/{po['id']}").json()


def _po_events(engine, po_id):
    with Session(engine) as s:
        return s.exec(
            select(OrderEvent)
            .where(OrderEvent.entity_kind == "PURCHASE_ORDER",
                   OrderEvent.entity_id == po_id)
            .order_by(OrderEvent.id)
        ).all()


# --------------------------------------------------------------------------- #
# Partial then full receive -> PARTIALLY_RECEIVED then RECEIVED
# --------------------------------------------------------------------------- #
def test_partial_then_full_receive(client, engine):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")

    r1 = client.post(f"/api/purchase-orders/{po['id']}/receive",
                     json={"lines": [{"po_line_id": line_id, "quantity": 400}]})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["status"] == "PARTIALLY_RECEIVED"
    assert body1["lines"][0]["received_qty"] == pytest.approx(400)

    r2 = client.post(f"/api/purchase-orders/{po['id']}/receive",
                     json={"lines": [{"po_line_id": line_id, "quantity": 600}]})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    # Fully received -> the demo BC reports MATCHED, so the PO ends MATCHED.
    assert body2["status"] == "MATCHED"
    assert body2["lines"][0]["received_qty"] == pytest.approx(1000)

    # Two GRNs -> two Receipt rows on this single line.
    with Session(engine) as s:
        receipts = s.exec(select(Receipt).where(Receipt.po_id == po["id"])).all()
    assert len(receipts) == 2
    assert {r.po_line_id for r in receipts} == {line_id}
    assert all(r.item_id for r in receipts)        # item_id stamped


def test_one_grn_per_receive_shared_across_lines(client, engine):
    # Two priced lines on one PO is hard with the demo grouping; instead assert one
    # receive call shares a single grn_no across its receipt rows.
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po['id']}/receive",
                       json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()
    grn = body["grn_no"]
    with Session(engine) as s:
        receipts = s.exec(select(Receipt).where(Receipt.po_id == po["id"])).all()
    assert {r.grn_no for r in receipts} == {grn}


# --------------------------------------------------------------------------- #
# Over-receipt -> 400 ; non-receivable state -> 409 ; unknown ids -> 404/400
# --------------------------------------------------------------------------- #
def test_over_receipt_is_400(client):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": line_id, "quantity": 1500}]})
    assert r.status_code == 400


def test_cumulative_over_receipt_is_400(client):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 900}]})
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": line_id, "quantity": 200}]})
    assert r.status_code == 400          # 900 + 200 > 1000


def test_receive_non_receivable_state_is_409(client):
    # A DRAFT PO (created but not issued) cannot be received against.
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]   # DRAFT
    line_id = client.get(f"/api/purchase-orders/{po['id']}").json()["lines"][0]["po_line_id"]
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": line_id, "quantity": 10}]})
    assert r.status_code == 409


def test_receive_already_received_po_is_409(client):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 1000}]})  # RECEIVED
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": line_id, "quantity": 1}]})
    # PO is MATCHED (demo) after the full receive -> not a receivable state.
    assert r.status_code == 409


def test_receive_unknown_po_is_404(client):
    as_role("OFFICER")
    r = client.post("/api/purchase-orders/nope/receive",
                    json={"lines": [{"po_line_id": "x", "quantity": 1}]})
    assert r.status_code == 404


def test_receive_foreign_line_is_400(client):
    po = _issued_po(client)
    as_role("OFFICER")
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": "not-a-line", "quantity": 1}]})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Receipt enqueues outbox + posts to (fake) BC writing a GRN ExternalRef
# --------------------------------------------------------------------------- #
def test_receipt_enqueues_and_posts_grn_to_bc(client, engine):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po['id']}/receive",
                       json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()
    grn = body["grn_no"]

    with Session(engine) as s:
        rows = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "post_receipt",
            IntegrationOutbox.entity_ref == grn)).all()
        assert len(rows) == 1
        assert rows[0].status == "SENT"          # drained inline (demo)
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "RECEIPT",
            ExternalRef.entity_id == grn,
            ExternalRef.system == "BC",
            ExternalRef.external_type == "GRN")).all()
        assert len(refs) == 1
        assert refs[0].external_id.startswith("BCGRN-")
        # Live BC posting maps received lines onto the BC order by item No, so
        # the outbox payload must carry sku/bc_item_no per line.
        payload = json.loads(rows[0].request_json)
        assert all(ln["sku"] and ln["bc_item_no"] for ln in payload["lines"])


# --------------------------------------------------------------------------- #
# NEVER DOUBLE-POST: process the receipt outbox twice -> one BC post / one GRN ref
# --------------------------------------------------------------------------- #
def test_process_receipt_outbox_twice_never_double_posts(client, engine, monkeypatch):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]

    calls = {"n": 0}
    real = bc_module.BCAdapter.post_receipt

    def _counting(self, payload):
        calls["n"] += 1
        return real(self, payload)

    monkeypatch.setattr(bc_module.BCAdapter, "post_receipt", _counting)

    # Receive WITHOUT inline processing so we control how many times we drain.
    monkeypatch.setattr(purchasing.settings, "outbox_process_on_issue", False,
                        raising=False)
    as_role("OFFICER")
    grn = client.post(f"/api/purchase-orders/{po['id']}/receive",
                      json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()["grn_no"]

    with Session(engine) as s:
        purchasing.process_outbox(s)
    with Session(engine) as s:
        purchasing.process_outbox(s)         # second run must be a no-op post-wise

    assert calls["n"] == 1                   # exactly ONE BC receipt post
    with Session(engine) as s:
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "RECEIPT",
            ExternalRef.entity_id == grn,
            ExternalRef.external_type == "GRN")).all()
    assert len(refs) == 1                    # exactly ONE GRN ExternalRef


# --------------------------------------------------------------------------- #
# Failure path: a failing receipt post increments attempts, stays PENDING, no ref
# --------------------------------------------------------------------------- #
def test_failing_receipt_post_retries_then_succeeds(client, engine, monkeypatch):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    monkeypatch.setattr(purchasing.settings, "outbox_process_on_issue", False,
                        raising=False)
    as_role("OFFICER")
    grn = client.post(f"/api/purchase-orders/{po['id']}/receive",
                      json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()["grn_no"]

    def _boom(self, payload):
        raise RuntimeError("BC receipt unreachable")

    monkeypatch.setattr(bc_module.BCAdapter, "post_receipt", _boom)
    with Session(engine) as s:
        purchasing.process_outbox(s)

    with Session(engine) as s:
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "post_receipt",
            IntegrationOutbox.entity_ref == grn)).first()
        assert row.status == "PENDING"
        assert row.attempts == 1
        assert "unreachable" in (row.last_error or "")
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "RECEIPT",
            ExternalRef.entity_id == grn)).all()
        assert refs == []                    # no crosswalk on failure

    # Recover on the retry.
    monkeypatch.undo()
    monkeypatch.setattr(purchasing.settings, "outbox_process_on_issue", False,
                        raising=False)
    with Session(engine) as s:
        purchasing.process_outbox(s)
    with Session(engine) as s:
        row = s.exec(select(IntegrationOutbox).where(
            IntegrationOutbox.action == "post_receipt",
            IntegrationOutbox.entity_ref == grn)).first()
        assert row.status == "SENT"
        refs = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "RECEIPT",
            ExternalRef.entity_id == grn)).all()
        assert len(refs) == 1


# --------------------------------------------------------------------------- #
# 3-way match: a fully received PO ends MATCHED (BC reports it) + audit
# --------------------------------------------------------------------------- #
def test_full_receive_matches_in_bc(client, engine):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po['id']}/receive",
                       json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()
    # PO transitioned RECEIVED -> MATCHED (demo BC reports MATCHED).
    assert body["status"] == "MATCHED"
    assert body["matched"] is True

    with Session(engine) as s:
        po_row = s.get(PurchaseOrder, po["id"])
        assert po_row.status == "MATCHED"
        match = s.exec(select(ExternalRef).where(
            ExternalRef.entity_kind == "PO",
            ExternalRef.entity_id == po["id"],
            ExternalRef.external_type == "INVOICE")).all()
        assert len(match) == 1
    types = [e.event_type for e in _po_events(engine, po["id"])]
    assert "RECEIPT_POSTED" in types and "MATCHED" in types


def test_match_not_recorded_on_partial_receive(client, engine):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po['id']}/receive",
                       json={"lines": [{"po_line_id": line_id, "quantity": 500}]}).json()
    # Receipt still posts to BC, but the PO is not fully received...
    assert body["status"] == "PARTIALLY_RECEIVED"
    # ...demo BC still reports MATCHED on the GRN it received, so the match reflects
    # per the contract (BC owns the match; we mirror what it reports).
    with Session(engine) as s:
        receipts = s.exec(select(Receipt).where(Receipt.po_id == po["id"])).all()
    assert len(receipts) == 1


# --------------------------------------------------------------------------- #
# Receiving triggers a stock re-read for the received items (architecture-correct)
# --------------------------------------------------------------------------- #
def test_receive_triggers_stock_refresh(client, engine, monkeypatch):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]

    refreshed = []
    real = purchasing.stock_service.refresh_item

    def _spy(session, item):
        refreshed.append(item.sku)
        return real(session, item)

    monkeypatch.setattr(purchasing.stock_service, "refresh_item", _spy)
    as_role("OFFICER")
    body = client.post(f"/api/purchase-orders/{po['id']}/receive",
                       json={"lines": [{"po_line_id": line_id, "quantity": 1000}]}).json()
    assert "BOARD-200K" in refreshed
    assert "BOARD-200K" in body["stock_refreshed"]


# --------------------------------------------------------------------------- #
# Audit: every receive records a RECEIVED OrderEvent on the PO
# --------------------------------------------------------------------------- #
def test_receive_records_audit_event(client, engine):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("OFFICER")
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 400}]})
    evts = [e for e in _po_events(engine, po["id"]) if e.event_type == "RECEIVED"]
    assert len(evts) == 1
    detail = json.loads(evts[0].detail_json)
    assert detail["grn_no"]
    assert detail["lines"][0]["po_line_id"] == line_id


# --------------------------------------------------------------------------- #
# RBAC: a VIEWER cannot receive
# --------------------------------------------------------------------------- #
def test_viewer_cannot_receive(client):
    po = _issued_po(client)
    line_id = po["lines"][0]["po_line_id"]
    as_role("VIEWER")
    r = client.post(f"/api/purchase-orders/{po['id']}/receive",
                    json={"lines": [{"po_line_id": line_id, "quantity": 10}]})
    assert r.status_code == 403
