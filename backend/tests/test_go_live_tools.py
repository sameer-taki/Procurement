"""Go-live tooling: vendor email override, grade preview, demo-data purge.

These are the pieces that turn a live BC sync into a usable app: the vendor
ORDER EMAIL lives in-app (GML's BC Vendor List exposes no E_Mail) and must
survive syncs; the grade preview lets the operator tune BC_PAPER_SKU_REGEX
against the real master before touching env; the purge removes the demo
catalog once live data exists without touching anything orders reference.
"""
import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.config import settings
from app.domain import stock_service
from app.gateway.models import (
    Customer, Forecast, Item, OrderEvent, UsageHistory, Vendor, VendorPrice,
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


def _live_bc(monkeypatch):
    """Force live BC mode (the purge gate + sync paths check adapter.use_fakes)."""
    monkeypatch.setattr(settings, "use_fake_adapters", False)
    monkeypatch.setattr(settings, "bc_base_url", "http://bc.test")
    monkeypatch.setattr(settings, "bc_username", "svc")
    monkeypatch.setattr(settings, "bc_password", "secret")


# --------------------------------------------------------------------------- #
# Vendor email override
# --------------------------------------------------------------------------- #
def _vendor_id(client, name_part):
    as_role("VIEWER")
    rows = client.get(f"/api/vendors?q={name_part}").json()
    assert rows, f"no vendor matching {name_part}"
    return rows[0]["id"]


def test_vendor_email_set_clear_and_rbac(client, engine):
    vid = _vendor_id(client, "Visy")

    as_role("VIEWER")
    assert client.patch(f"/api/vendors/{vid}", json={"email": "x@y.example"}).status_code == 403

    as_role("OFFICER")
    r = client.patch(f"/api/vendors/{vid}", json={"email": "orders@visy.example.fj"})
    assert r.status_code == 200
    assert r.json()["email"] == "orders@visy.example.fj"

    # audited
    with Session(engine) as s:
        ev = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "VENDOR",
            OrderEvent.event_type == "VENDOR_EMAIL_UPDATED",
        )).all()
        assert ev

    # clear with blank
    r = client.patch(f"/api/vendors/{vid}", json={"email": " "})
    assert r.status_code == 200 and r.json()["email"] is None

    assert client.patch(f"/api/vendors/{vid}", json={"email": "not-an-email"}).status_code == 400
    assert client.patch("/api/vendors/nope", json={"email": "a@b.c"}).status_code == 404


def test_sync_does_not_wipe_manual_vendor_email(engine, monkeypatch):
    """BC returns email=None (E_Mail not on the page); the sync must keep the
    in-app address, and only overwrite once BC really supplies one."""
    _live_bc(monkeypatch)
    with Session(engine) as s:
        vendor = s.exec(select(Vendor).where(Vendor.bc_vendor_no == "V-2001")).first()
        vendor.email = "manual@visy.example"
        s.add(vendor)
        s.commit()

        monkeypatch.setattr(stock_service.bc, "list_vendors", lambda: [
            {"bc_vendor_no": "V-2001", "name": "Visy Board", "email": None},
        ])
        stock_service.sync_vendors(s)
        s.refresh(vendor)
        assert vendor.email == "manual@visy.example"     # preserved

        monkeypatch.setattr(stock_service.bc, "list_vendors", lambda: [
            {"bc_vendor_no": "V-2001", "name": "Visy Board", "email": "bc@visy.example"},
        ])
        stock_service.sync_vendors(s)
        s.refresh(vendor)
        assert vendor.email == "bc@visy.example"         # BC value wins when present


def test_sync_does_not_wipe_manual_customer_email(engine, monkeypatch):
    _live_bc(monkeypatch)
    with Session(engine) as s:
        cust = s.exec(select(Customer).where(Customer.bc_customer_no == "C-1001")).first()
        cust.email = "manual@fijiwater.example"
        s.add(cust)
        s.commit()
        monkeypatch.setattr(stock_service.bc, "list_customers", lambda: [
            {"bc_customer_no": "C-1001", "name": "Fiji Water", "email": None},
        ])
        stock_service.sync_customers(s)
        s.refresh(cust)
        assert cust.email == "manual@fijiwater.example"   # preserved

        monkeypatch.setattr(stock_service.bc, "list_customers", lambda: [
            {"bc_customer_no": "C-1001", "name": "Fiji Water", "email": "bc@fw.example"},
        ])
        stock_service.sync_customers(s)
        s.refresh(cust)
        assert cust.email == "bc@fw.example"              # BC value wins when present


# --------------------------------------------------------------------------- #
# Grade preview
# --------------------------------------------------------------------------- #
def test_grade_preview_matches_and_reports(admin_client):
    # The demo catalog's roll SKUs follow <grade>-<deckle>; the default pattern
    # must classify them and extract both attributes.
    r = admin_client.get(
        "/api/planning/grade-preview",
        params={"regex": r"^([A-Z]{2,4}\d{2,3})-(\d{3,4})$"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match_count"] >= 6
    by_sku = {m["sku"]: m for m in body["sample"]}
    assert by_sku["CWT140-1400"]["grade"] == "CWT140"
    assert by_sku["CWT140-1400"]["deckle_mm"] == 1400
    assert "CWT140" in body["grades"]
    assert body["total_items"] > body["match_count"]     # non-paper items exist


def test_grade_preview_grade_only_pattern_leaves_deckle_none(admin_client):
    r = admin_client.get(
        "/api/planning/grade-preview",
        params={"regex": r"^([A-Z]{2,4}\d{2,3})-1400$"},
    )
    body = r.json()
    assert body["match_count"] >= 1
    assert all(m["deckle_mm"] is None for m in body["sample"])


def test_grade_preview_rejects_bad_or_oversized_regex(admin_client):
    assert admin_client.get(
        "/api/planning/grade-preview", params={"regex": "(unclosed"}
    ).status_code == 400
    assert admin_client.get(
        "/api/planning/grade-preview", params={"regex": "a" * 201}
    ).status_code == 400


def test_grade_preview_requires_planner_role(client):
    as_role("VIEWER")
    r = client.get("/api/planning/grade-preview",
                   params={"regex": r"^([A-Z]+\d+)$"})
    assert r.status_code == 403


def test_grade_preview_groupless_pattern_reports_ungraded_not_graded(admin_client):
    """A pattern with NO capture group matches but grades nothing in the real
    sync (parse_paper_match -> grade None). The preview must say so instead of
    reporting the SKUs as classified — the exact trap for this tenant, where an
    operator writes ^[A-Z]{2,4}\\d{2,3}-\\d{3,4}$ without parentheses."""
    r = admin_client.get(
        "/api/planning/grade-preview",
        params={"regex": r"^[A-Z]{2,4}\d{2,3}-\d{3,4}$"},   # no capture group
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match_count"] >= 6
    assert body["ungraded_matches"] == body["match_count"]  # sync would grade none
    assert body["distinct_grades"] == 0
    assert body["grades"] == [] and body["sample"] == []


def test_grade_preview_partial_group_participation_no_500(admin_client):
    """Alternation where group 1 only participates in one branch: matches of the
    other branch must count as ungraded — not crash sorted() with a None."""
    r = admin_client.get(
        "/api/planning/grade-preview",
        params={"regex": r"^(?:(CWT\d+)-\d+|BX\d+-\d+)$"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ungraded_matches"] >= 1                    # the BX branch
    assert body["grades"] and all(g for g in body["grades"])
    assert all(m["grade"] for m in body["sample"])


def test_grade_preview_kills_catastrophic_backtracking(admin_client, engine):
    """ReDoS guard: a nested-quantifier pattern against a long SKU must come
    back as a 400 within the timeout budget — not pin the worker forever."""
    import time

    with Session(engine) as s:
        s.add(Item(sku="A" * 60, name="pathological subject",
                   item_type="MATERIAL"))
        s.commit()
    t0 = time.monotonic()
    r = admin_client.get(
        "/api/planning/grade-preview",
        params={"regex": r"^(([A-Z0-9]+)+)+-(\d{3,4})$"},
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 400
    assert "long" in r.json()["detail"].lower()
    assert elapsed < 30                                     # killed, not hung


# --------------------------------------------------------------------------- #
# Demo purge
# --------------------------------------------------------------------------- #
def test_purge_refuses_in_demo_mode(client):
    as_role("ADMIN")
    assert client.post("/api/admin/purge-demo-data").status_code == 409


def test_purge_removes_demo_data_but_keeps_referenced_items(client, engine, monkeypatch):
    from app.gateway.models import BomHeader, BomLine, StockSnapshot

    # An order references one demo item: that item must survive the purge.
    as_role("REQUESTER")
    r = client.post("/api/requisitions", json={
        "cost_center": "CC-100", "lines": [{"sku": "BOARD-200K", "quantity": 5}],
    })
    assert r.status_code in (200, 201), r.text

    with Session(engine) as s:
        doomed_ids = [it.id for it in s.exec(
            select(Item).where(Item.sku.in_(["CWT140-1400", "CTN-FIJIWATER-1L", "BOX-RSC-A"]))
        ).all()]
        assert len(doomed_ids) == 3

    _live_bc(monkeypatch)
    as_role("ADMIN")
    r = client.post("/api/admin/purge-demo-data")
    assert r.status_code == 200, r.text
    summary = r.json()

    assert "BOARD-200K" in summary["skipped_items"]
    assert summary["items"] > 0 and summary["vendors"] > 0
    assert summary["customers"] >= 4

    with Session(engine) as s:
        skus = set(s.exec(select(Item.sku)).all())
        assert "BOARD-200K" in skus                       # referenced -> kept
        assert "CWT140-1400" not in skus                  # demo roll purged
        assert "CTN-FIJIWATER-1L" not in skus
        assert s.exec(select(Vendor).where(Vendor.bc_vendor_no == "V-2001")).first() is None
        assert s.exec(select(Customer).where(Customer.bc_customer_no == "C-1001")).first() is None
        # No dangling dependents for purged items (snapshots, BOM headers+lines).
        assert s.exec(select(StockSnapshot).where(
            StockSnapshot.item_id.in_(doomed_ids))).first() is None
        assert s.exec(select(BomHeader).where(
            BomHeader.parent_item_id.in_(doomed_ids))).first() is None
        assert s.exec(select(BomLine).where(
            BomLine.component_id.in_(doomed_ids))).first() is None
        assert s.exec(select(Forecast)).first() is None   # demo forecast was for a purged item
        assert s.exec(select(UsageHistory)).first() is None
        kept = s.exec(select(Item).where(Item.sku == "BOARD-200K")).first()
        prices = s.exec(select(VendorPrice).where(VendorPrice.item_id == kept.id)).all()
        assert prices == []                               # its prices were demo vendors'
        ev = s.exec(select(OrderEvent).where(
            OrderEvent.event_type == "DEMO_DATA_PURGED")).first()
        assert ev is not None


def test_purge_skips_vendor_and_item_referenced_by_po(client, engine, monkeypatch):
    """A PO pins BOTH its vendor and its line items: neither may be purged.
    (Build the PO through the real flow in demo mode, then purge in live mode.)"""
    as_role("REQUESTER")
    req_id = client.post("/api/requisitions", json={
        "cost_center": "CC-100",
        "lines": [{"sku": "BOARD-200K", "quantity": 1000}],
    }).json()["id"]
    client.post(f"/api/requisitions/{req_id}/submit")
    as_role("ADMIN", email="admin")
    assert client.post(f"/api/requisitions/{req_id}/approve").json()["status"] == "APPROVED"
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    po_vendor = po["vendor"] if isinstance(po.get("vendor"), str) else po.get("vendor_name")

    _live_bc(monkeypatch)
    as_role("ADMIN")
    summary = client.post("/api/admin/purge-demo-data").json()

    assert "BOARD-200K" in summary["skipped_items"]        # on a PO line
    assert summary["skipped_vendors"]                      # the PO's vendor kept
    with Session(engine) as s:
        assert s.exec(select(Item).where(Item.sku == "BOARD-200K")).first() is not None
        kept_vendors = {v.name for v in s.exec(select(Vendor)).all()}
        assert summary["skipped_vendors"][0] in kept_vendors
        if po_vendor:
            assert po_vendor in kept_vendors


def test_purge_is_idempotent(client, engine, monkeypatch):
    _live_bc(monkeypatch)
    as_role("ADMIN")
    first = client.post("/api/admin/purge-demo-data").json()
    second = client.post("/api/admin/purge-demo-data").json()
    assert first["items"] > 0 and first["customers"] >= 4  # first pass really purged
    assert second["items"] == 0 and second["vendors"] == 0 and second["customers"] == 0
