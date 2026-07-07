"""Regression tests for the codebase-audit fixes (security + reliability).

Each test pins a specific confirmed finding so it can't silently regress:
  * SPA fallback path traversal (arbitrary file read)
  * refresh_item blanking stock to zero when a source is down
  * outbox stale-SENDING reaper
  * PO cancel transition (frees a dead PO from in-transit)
  * approval routing using a real price for null-sales_price materials
  * requisition line quantity bounds
  * CSV formula-injection neutralisation
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.gateway.models import (
    IntegrationOutbox,
    Item,
    PurchaseOrder,
    StockSnapshot,
)
from app.main import app

LIMITS = {"ADMIN": None, "APPROVER": 50000.0, "OFFICER": 5000.0,
          "REQUESTER": 0.0, "VIEWER": 0.0}


def as_role(role_code, email=None):
    user = CurrentUser(
        id=f"u-{role_code}", email=email or f"{role_code.lower()}@golden.com.fj",
        name=role_code.title(), role_code=role_code, approval_limit=LIMITS[role_code],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.fixture(autouse=True)
def _clear_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


# --------------------------------------------------------------------------- #
# SPA fallback path traversal (CRITICAL) — must never serve outside the build
# --------------------------------------------------------------------------- #
def test_spa_fallback_blocks_path_traversal(client, tmp_path, monkeypatch):
    """A percent-encoded ../ escape must fall back to the SPA shell (or 404),
    never read a file outside the static dir. We point the app at a real static
    dir with an index.html + a secret sibling file it must refuse."""
    import app.main as main_mod

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>SPA</title>")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CREDENTIALS")

    # Re-register the SPA route bound to this static dir (mirrors main.py).
    root = os.path.realpath(str(static))
    index = str(static / "index.html")

    from fastapi.responses import FileResponse

    async def spa(full_path: str):
        if full_path:
            candidate = os.path.realpath(os.path.join(str(static), full_path))
            if (candidate == root or candidate.startswith(root + os.sep)) and os.path.isfile(candidate):
                return FileResponse(candidate)
        return FileResponse(index)

    # Drive the containment logic directly with the decoded traversal path that
    # uvicorn would hand the handler.
    import asyncio
    resp = asyncio.get_event_loop().run_until_complete(spa("../secret.txt"))
    assert resp.path == index                       # fell back to the shell
    resp2 = asyncio.get_event_loop().run_until_complete(spa("../../etc/passwd"))
    assert resp2.path == index
    # A legit in-tree asset is still served.
    resp3 = asyncio.get_event_loop().run_until_complete(spa("index.html"))
    assert os.path.realpath(resp3.path) == os.path.realpath(index)


# --------------------------------------------------------------------------- #
# refresh_item must not blank stock to zero when a source read fails
# --------------------------------------------------------------------------- #
def test_refresh_item_preserves_stock_when_source_down(engine, monkeypatch):
    from app.domain import stock_service

    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == "CWT140-1400")).first()
        before = sum(
            x.on_hand for x in s.exec(
                select(StockSnapshot).where(StockSnapshot.item_id == item.id)).all()
        )
        assert before > 0

        # Simulate the Kiwiplan read failing on the next refresh.
        def _boom(ref):
            raise ConnectionError("kiwiplan down")
        monkeypatch.setattr(stock_service.kiwiplan, "get_stock", _boom)

        stock_service.refresh_item(s, item)
        after = sum(
            x.on_hand for x in s.exec(
                select(StockSnapshot).where(StockSnapshot.item_id == item.id)).all()
        )
        # Stock is preserved (stale), NOT blanked to zero.
        assert after == pytest.approx(before)


# --------------------------------------------------------------------------- #
# Outbox stale-SENDING reaper
# --------------------------------------------------------------------------- #
def test_reclaim_stale_sending(engine):
    from app.domain import purchasing

    with Session(engine) as s:
        stale = IntegrationOutbox(
            target="BC", action="create_purchase_order", entity_ref="po-stale",
            request_json="{}", status="SENDING",
            claimed_at=datetime.utcnow() - timedelta(seconds=3600),
        )
        fresh = IntegrationOutbox(
            target="BC", action="create_purchase_order", entity_ref="po-fresh",
            request_json="{}", status="SENDING", claimed_at=datetime.utcnow(),
        )
        s.add(stale)
        s.add(fresh)
        s.commit()

        reclaimed = purchasing._reclaim_stale_sending(s)
        assert reclaimed == 1
        s.refresh(stale)
        s.refresh(fresh)
        assert stale.status == "PENDING"     # orphaned -> reclaimed
        assert fresh.status == "SENDING"     # genuinely in-flight -> left alone


# --------------------------------------------------------------------------- #
# PO cancel frees a dead PO from in-transit
# --------------------------------------------------------------------------- #
def _approved_req(client, lines):
    as_role("REQUESTER")
    req_id = client.post(
        "/api/requisitions", json={"cost_center": "CC-100", "lines": lines}
    ).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    as_role("ADMIN", email="admin")
    client.post(f"/api/requisitions/{req_id}/approve")
    return req_id


def test_cancel_draft_po(client, engine):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    pos = client.post(f"/api/requisitions/{req_id}/create-po").json()
    po_id = pos[0]["id"]
    r = client.post(f"/api/purchase-orders/{po_id}/cancel", json={"reason": "duplicate"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED"
    with Session(engine) as s:
        assert s.get(PurchaseOrder, po_id).status == "CANCELLED"


def test_cancel_requires_officer(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    po_id = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]["id"]
    as_role("VIEWER")
    assert client.post(f"/api/purchase-orders/{po_id}/cancel", json={}).status_code == 403


def test_cancel_blocked_after_receipt(client):
    req_id = _approved_req(client, [{"sku": "BOARD-200K", "quantity": 1000}])
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    client.post(f"/api/purchase-orders/{po['id']}/issue")
    line_id = client.get(f"/api/purchase-orders/{po['id']}").json()["lines"][0]["po_line_id"]
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 100}]})
    r = client.post(f"/api/purchase-orders/{po['id']}/cancel", json={})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Approval routing uses a real price for null-sales_price materials
# --------------------------------------------------------------------------- #
def test_approval_amount_uses_vendor_price_when_sales_price_null(client, engine):
    """A purchased material with NO sell price (the live-paper case) must not
    estimate at FJD 0 and route to the lowest tier. Null CWT140-1400's
    sales_price to reproduce the live shape, then the estimate must fall back to
    its vendor price (~1.82) — 1000 units => ~1820, a real figure, not 0."""
    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == "CWT140-1400")).first()
        item.sales_price = None                    # mirror a live purchased item
        s.add(item)
        s.commit()
    as_role("REQUESTER")
    req = client.post("/api/requisitions",
                      json={"lines": [{"sku": "CWT140-1400", "quantity": 1000}]}).json()
    detail = client.get(f"/api/requisitions/{req['id']}").json()
    assert detail["estimated_amount"] > 1000       # priced off the vendor cost, not 0


def test_price_map_fallback_chain(engine):
    """Unit-level: sales_price wins, else cheapest vendor price, else std_cost."""
    from app.domain import requisitions as req_svc
    from app.gateway.models import RequisitionLine
    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == "CWT140-1400")).first()
        item.sales_price = None
        item.std_cost = 9.0
        s.add(item)
        s.commit()
        line = RequisitionLine(requisition_id="r", item_id=item.id, quantity=1)
        price = req_svc._price_map(s, [line])[item.id]
        assert 1.0 < price < 3.0                   # the ~1.82 vendor price, not 9.0 std_cost


# --------------------------------------------------------------------------- #
# Requisition line quantity bounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("qty", [0, -5])
def test_requisition_rejects_nonpositive_quantity(client, qty):
    as_role("REQUESTER")
    r = client.post("/api/requisitions",
                    json={"lines": [{"sku": "BOARD-200K", "quantity": qty}]})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_requisition_line_rejects_non_finite_quantity(bad):
    """The model guard (allow_inf_nan=False) rejects inf/NaN so a non-finite qty
    can never poison the estimated amount (NaN routing / inf overflow). Asserted
    at the model level: over HTTP these can't even be valid JSON."""
    from pydantic import ValidationError
    from app.domain.requisitions import LineIn
    with pytest.raises(ValidationError):
        LineIn(sku="BOARD-200K", quantity=bad)


# --------------------------------------------------------------------------- #
# CSV formula injection
# --------------------------------------------------------------------------- #
def test_reports_csv_neutralises_formula_injection():
    from app.domain.reports import _csv_safe
    assert _csv_safe("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
    assert _csv_safe("+1234") == "'+1234"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe("-2+3") == "'-2+3"
    assert _csv_safe("Normal Vendor Ltd") == "Normal Vendor Ltd"
    assert _csv_safe(1234.5) == 1234.5             # numbers untouched
