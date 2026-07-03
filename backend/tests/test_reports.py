"""Operational reports: PO register, receipt log, spend by month — JSON + CSV.

Drives the real lifecycle against the demo catalog (requisition -> approve ->
PO -> issue -> receive) so the registers report actual canonical rows, then
asserts both serializations share columns (one serializer contract).
"""
import csv
import io

import pytest

from app.auth.deps import CurrentUser, get_current_user
from app.main import app


@pytest.fixture(autouse=True)
def _clear_override():
    yield
    app.dependency_overrides.pop(get_current_user, None)


def as_role(role_code, limit=None):
    user = CurrentUser(
        id=f"u-{role_code}", email=f"{role_code.lower()}@golden.com.fj",
        name=role_code.title(), role_code=role_code, approval_limit=limit,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.fixture()
def received_po(client):
    """A PO taken through issue + partial receive; returns its detail dict."""
    as_role("OFFICER")
    req = client.post("/api/requisitions", json={
        "lines": [{"sku": "BOARD-200K", "quantity": 1000}],
    }).json()
    client.post(f"/api/requisitions/{req['id']}/submit")
    as_role("ADMIN")
    client.post(f"/api/requisitions/{req['id']}/approve")
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req['id']}/create-po").json()[0]
    client.post(f"/api/purchase-orders/{po['id']}/issue")
    detail = client.get(f"/api/purchase-orders/{po['id']}").json()
    line_id = detail["lines"][0]["po_line_id"]
    client.post(f"/api/purchase-orders/{po['id']}/receive",
                json={"lines": [{"po_line_id": line_id, "quantity": 400}]})
    return client.get(f"/api/purchase-orders/{po['id']}").json()


def test_po_register_reports_fulfilment(client, received_po):
    as_role("VIEWER")
    out = client.get("/api/reports/purchase-orders").json()
    row = next(r for r in out["rows"] if r["number"] == received_po["number"])
    assert row["status"] == "PARTIALLY_RECEIVED"
    assert row["ordered_qty"] == pytest.approx(1000)
    assert row["received_qty"] == pytest.approx(400)
    assert row["received_pct"] == pytest.approx(0.4)
    assert row["bc_po_no"].startswith("BCPO-")     # demo BC posting crosswalk
    assert out["columns"][0] == "number"


def test_po_register_status_filter(client, received_po):
    as_role("VIEWER")
    out = client.get("/api/reports/purchase-orders?status=CLOSED").json()
    assert out["rows"] == []


def test_receipt_log_traces_to_po_and_bc(client, received_po):
    as_role("VIEWER")
    out = client.get("/api/reports/receipts").json()
    assert out["count"] == 1
    row = out["rows"][0]
    assert row["po_number"] == received_po["number"]
    assert row["sku"] == "BOARD-200K"
    assert row["quantity"] == pytest.approx(400)
    assert row["bc_grn_no"].startswith("BCGRN-")


def test_spend_by_month_prices_received_qty(client, received_po):
    as_role("VIEWER")
    out = client.get("/api/reports/spend-by-month").json()
    assert out["count"] == 1
    row = out["rows"][0]
    unit_price = received_po["lines"][0]["unit_price"]
    assert row["spend"] == pytest.approx(400 * unit_price)
    assert row["vendor"] == received_po["vendor"]["name"]


def test_csv_matches_json_columns(client, received_po):
    as_role("VIEWER")
    for path in ("/api/reports/purchase-orders", "/api/reports/receipts",
                 "/api/reports/spend-by-month"):
        js = client.get(path).json()
        r = client.get(f"{path}?format=csv")
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        parsed = list(csv.reader(io.StringIO(r.text)))
        assert parsed[0] == js["columns"]          # same serializer contract
        assert len(parsed) - 1 == js["count"]


def test_reports_require_auth(client):
    assert client.get("/api/reports/purchase-orders").status_code == 401
