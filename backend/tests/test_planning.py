"""Paper planning — the SOP's 3-month cover by grade/deckle (Order Page).

Two layers, mirroring how test_bom.py/test_bom_service.py split engine vs service:
  * pure engine (app/gateway/planning.py): trailing_average / months_of_stock /
    order_quantity / round_up_to_block / consolidate_containers / plan_orders —
    no DB, plain values.
  * service + API (app/domain/planning.py): the Order Page, usage import and
    suggest-orders against the conftest-seeded demo catalog (refresh_all seeds
    items, stock, vendors, BOMs, usage_history AND the demo forecast).

Worked example — computed against the SEEDED demo data (fakes.py):

Forecast basis (SOP steps 1-2/4): 'Fiji Water' x CTN-FIJIWATER-1L over the
current 3-month window = 42000+45000+40000 = 127000 cartons, exploded through
the carton BOM to KG per grade/deckle:
  CWT140-1400 gross = 127000*0.42*1.04 = 55473.6 -> monthly 18491.2
  RF135-1000  gross = 127000*0.35*1.05 = 46672.5 -> monthly 15557.5
  BX186-1400  gross = 127000*0.38*1.04 = 50190.4 -> monthly 16730.13

History basis (step 5, no forecast): mean of the last 3 months of usage_history
(seeded from fakes.USAGE_KG_BY_SKU, trailing-6 tail):
  HP140-1490  (21800+22600+22100)/3 = 22166.67
  CWT140-1950 (7900+8100+7700)/3    = 7900
  BX200-1950  (4900+5200+4900)/3    = 5000

Order determination (step 6 / SOP §8):
  requirement = 3*monthly + allocated - on_hand - in_transit  (never negative)
  CWT140-1400 55473.6+6000-30000 = 31473.6   RF135-1000 46672.5+3000-20000 = 29672.5
  HP140-1490  66500+8000-40000   = 34500     BX200-1950 15000+1500-4000    = 12500
  CWT140-1950 23700+2000-30000 < 0 -> 0      BX186-1400 50190.4+5000-60000 < 0 -> 0

Container consolidation (25t = 1x40ft FCL, per vendor; slack tops up the
largest-requirement line):
  Visy Board:          31473.6 -> 2 FCL = 50000 (CWT140-1400 order_kg 50000)
  Changle Numat (CSC): 34500+29672.5+12500 = 76672.5 -> 4 FCL = 100000
                       (slack 23327.5 onto HP140-1490 -> order_kg 57827.5)

months_of_stock = (on_hand + in_transit) / monthly (None when monthly <= 0):
  CWT140-1400 1.62, HP140 1.80, RF135 1.29, BX200 0.8 (below 3-month cover);
  CWT140-1950 3.80, BX186 3.59 (covered)  ->  below_cover = 4.
"""
import json

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.domain import planning as planning_service
from app.domain.planning import forward_periods, trailing_periods
from app.gateway.planning import (
    KG_PER_FCL,
    ContainerPlan,
    PlanLine,
    consolidate_containers,
    months_of_stock,
    order_quantity,
    plan_orders,
    round_up_to_block,
    trailing_average,
)
from app.gateway.models import (
    Forecast,
    Item,
    ItemType,
    OrderEvent,
    Requisition,
    RequisitionLine,
    UsageHistory,
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


def _rows_by_sku(page):
    return {r["sku"]: r for r in page["rows"]}


def _plans_by_vendor(page_or_req):
    return {p["vendor"]: p for p in page_or_req["container_plans"]}


def _req_count(engine):
    with Session(engine) as s:
        return len(s.exec(select(Requisition)).all())


# --------------------------------------------------------------------------- #
# Engine: trailing_average (empty window is 'no basis', zero months count)
# --------------------------------------------------------------------------- #
def test_trailing_average_of_window():
    assert trailing_average([21800, 22600, 22100]) == pytest.approx(66500 / 3)


def test_trailing_average_counts_zero_months():
    # A slow month is real data: (0+0+600)/3 = 200, not 600.
    assert trailing_average([0, 0, 600]) == pytest.approx(200.0)


def test_trailing_average_empty_window_is_zero():
    assert trailing_average([]) == 0.0


# --------------------------------------------------------------------------- #
# Engine: months_of_stock (in-transit counts; undefined -> None, not inf)
# --------------------------------------------------------------------------- #
def test_months_of_stock_basic():
    assert months_of_stock(4000, 0, 5000) == pytest.approx(0.8)


def test_months_of_stock_counts_in_transit():
    # (4000 + 25000) / 5000 = 5.8 — a confirmed FCL on the water is cover.
    assert months_of_stock(4000, 25000, 5000) == pytest.approx(5.8)


def test_months_of_stock_none_without_usage_basis():
    assert months_of_stock(30000, 0, 0.0) is None
    assert months_of_stock(30000, 0, -1.0) is None


# --------------------------------------------------------------------------- #
# Engine: order_quantity (SOP §8; never negative)
# --------------------------------------------------------------------------- #
def test_order_quantity_worked_example():
    # CWT140-1400: 3*18491.2 + 6000 - 30000 - 0 = 31473.6
    assert order_quantity(18491.2, 6000, 30000, 0) == pytest.approx(31473.6)


def test_order_quantity_nets_in_transit():
    # BX200-1950 with 25000 on the water: 15000 + 1500 - 4000 - 25000 < 0 -> 0
    assert order_quantity(5000, 1500, 4000, 25000) == 0.0


def test_order_quantity_never_negative():
    # CWT140-1950: 23700 + 2000 - 30000 = -4300 -> clamped to 0
    assert order_quantity(7900, 2000, 30000, 0) == 0.0


def test_order_quantity_cover_months_override():
    assert order_quantity(1000, 0, 0, 0, cover_months=2) == pytest.approx(2000)


# --------------------------------------------------------------------------- #
# Engine: round_up_to_block (25t blocks; exact multiple stays; zero block = none)
# --------------------------------------------------------------------------- #
def test_round_up_to_block_rounds_up():
    assert round_up_to_block(100) == pytest.approx(25000)
    assert round_up_to_block(25001) == pytest.approx(50000)


def test_round_up_to_block_exact_multiple_not_bumped():
    # 50000 is exactly 2 blocks — the epsilon guard must not push it to 3.
    assert round_up_to_block(50000.0) == pytest.approx(50000)
    assert round_up_to_block(KG_PER_FCL) == pytest.approx(KG_PER_FCL)


def test_round_up_to_block_zero_or_negative_kg():
    assert round_up_to_block(0) == 0.0
    assert round_up_to_block(-10) == 0.0


def test_round_up_to_block_zero_block_means_no_rounding():
    # Non-container-bound materials keep their raw requirement.
    assert round_up_to_block(1234.5, block_kg=0) == pytest.approx(1234.5)
    assert round_up_to_block(1234.5, block_kg=None) == pytest.approx(1234.5)


# --------------------------------------------------------------------------- #
# Engine: consolidate_containers (vendor total -> whole FCLs, slack on largest)
# --------------------------------------------------------------------------- #
def test_consolidate_slack_tops_up_largest_line():
    # 30000 + 10000 = 40000 -> 2 FCL = 50000; slack 10000 onto the 30000 line.
    plan = consolidate_containers([PlanLine("big", 30000.0), PlanLine("small", 10000.0)])
    assert isinstance(plan, ContainerPlan)
    assert plan.containers == 2
    assert plan.total_kg == pytest.approx(50000)
    lines = {ln.item_id: ln for ln in plan.lines}
    assert lines["big"].order_kg == pytest.approx(40000)
    assert lines["small"].order_kg == pytest.approx(10000)   # untouched
    assert sum(ln.order_kg for ln in plan.lines) == pytest.approx(plan.total_kg)


def test_consolidate_exact_fill_has_no_slack():
    plan = consolidate_containers([PlanLine("a", 25000.0), PlanLine("b", 25000.0)])
    assert plan.containers == 2
    assert plan.total_kg == pytest.approx(50000)
    assert all(ln.order_kg == pytest.approx(ln.requirement_kg) for ln in plan.lines)


def test_consolidate_whole_fcl_totals():
    # 34500 + 29672.5 + 12500 = 76672.5 -> 4 FCL = 100000; slack 23327.5 on 34500.
    plan = consolidate_containers([
        PlanLine("hp", 34500.0), PlanLine("rf", 29672.5), PlanLine("bx", 12500.0),
    ])
    assert plan.containers == 4
    assert plan.total_kg == pytest.approx(100000)
    lines = {ln.item_id: ln for ln in plan.lines}
    assert lines["hp"].order_kg == pytest.approx(57827.5)    # 34500 + 23327.5
    assert lines["rf"].order_kg == pytest.approx(29672.5)
    assert lines["bx"].order_kg == pytest.approx(12500)


def test_consolidate_no_requirement_returns_none():
    assert consolidate_containers([]) is None
    assert consolidate_containers([PlanLine("a", 0.0)]) is None
    assert consolidate_containers([PlanLine("a", -5.0)]) is None


def test_consolidate_zero_block_no_container_discipline():
    plan = consolidate_containers(
        [PlanLine("a", 1200.0), PlanLine("b", 300.0)], block_kg=0)
    assert plan.containers == 0
    assert plan.total_kg == pytest.approx(1500.0)            # raw total, no slack
    assert all(ln.order_kg == pytest.approx(ln.requirement_kg) for ln in plan.lines)


def test_consolidate_drops_zero_lines_keeps_live_ones():
    plan = consolidate_containers([PlanLine("dead", 0.0), PlanLine("live", 100.0)])
    assert [ln.item_id for ln in plan.lines] == ["live"]
    assert plan.containers == 1
    assert plan.lines[0].order_kg == pytest.approx(25000)    # 100 + all the slack


# --------------------------------------------------------------------------- #
# Engine: plan_orders (multi-vendor; empty vendors dropped; sorted)
# --------------------------------------------------------------------------- #
def test_plan_orders_multi_vendor():
    plans = plan_orders({
        "v-visy": {"i-cwt": 31473.6},
        "v-csc": {"i-hp": 34500.0, "i-rf": 29672.5, "i-bx": 12500.0},
        "v-empty": {},                                       # nothing to order
    })
    assert [p.vendor_id for p in plans] == ["v-csc", "v-visy"]
    csc, visy = plans
    assert csc.containers == 4
    assert csc.total_kg == pytest.approx(100000)
    assert visy.containers == 2
    assert visy.total_kg == pytest.approx(50000)
    assert visy.lines[0].order_kg == pytest.approx(50000)


def test_plan_orders_vendorless_requirement_sorts_first():
    plans = plan_orders({None: {"i": 100.0}, "v-1": {"j": 100.0}})
    assert [p.vendor_id for p in plans] == [None, "v-1"]


# --------------------------------------------------------------------------- #
# Build an ISSUED paper PO so its remaining volume shows as in-transit.
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


def _issued_paper_po(client, sku="BX200-1950", quantity=25000):
    req_id = _approved_req(client, [{"sku": sku, "quantity": quantity}])
    as_role("OFFICER")
    po = client.post(f"/api/requisitions/{req_id}/create-po").json()[0]
    issued = client.post(f"/api/purchase-orders/{po['id']}/issue").json()
    assert issued["status"] == "ACKNOWLEDGED", issued        # open -> in transit
    return client.get(f"/api/purchase-orders/{po['id']}").json()


# --------------------------------------------------------------------------- #
# Order Page: shape, window, sort order (grade then deckle)
# --------------------------------------------------------------------------- #
def test_order_page_shape_and_sort(client):
    as_role("VIEWER")
    page = client.get("/api/planning/order-page").json()
    assert page["cover_months"] == 3
    assert page["kg_per_fcl"] == pytest.approx(25000)
    assert page["window"] == forward_periods(3)              # current month + 2
    assert [r["sku"] for r in page["rows"]] == [
        "BX186-1400", "BX200-1950", "CWT140-1400",
        "CWT140-1950", "HP140-1490", "RF135-1000",
    ]


# --------------------------------------------------------------------------- #
# Order Page: basis selection — FORECAST beats HISTORY beats NONE
# --------------------------------------------------------------------------- #
def test_order_page_basis_selection(client, engine):
    # A grade with no forecast AND no usage rows -> basis NONE (added fresh).
    with Session(engine) as s:
        s.add(Item(sku="TL125-1600", name="Test Liner 125gsm 1600mm",
                   item_type=ItemType.MATERIAL, uom="KG",
                   grade="TL125", deckle_mm=1600))
        s.commit()
    as_role("VIEWER")
    rows = _rows_by_sku(client.get("/api/planning/order-page").json())
    # Forecasted paper (in the CTN-FIJIWATER-1L bill) plans off the forecast...
    assert rows["CWT140-1400"]["basis"] == "FORECAST"
    assert rows["RF135-1000"]["basis"] == "FORECAST"
    assert rows["BX186-1400"]["basis"] == "FORECAST"
    # ...everything else falls back to trailing usage...
    assert rows["CWT140-1950"]["basis"] == "HISTORY"
    assert rows["HP140-1490"]["basis"] == "HISTORY"
    assert rows["BX200-1950"]["basis"] == "HISTORY"
    # ...and no data at all is flagged, not guessed.
    none_row = rows["TL125-1600"]
    assert none_row["basis"] == "NONE"
    assert none_row["monthly_usage"] == pytest.approx(0.0)
    assert none_row["months_of_stock"] is None               # undefined, not inf
    assert none_row["requirement_kg"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Order Page: exact FORECAST figures (worked example)
# --------------------------------------------------------------------------- #
def test_order_page_forecast_figures(client):
    as_role("VIEWER")
    rows = _rows_by_sku(client.get("/api/planning/order-page").json())

    cwt = rows["CWT140-1400"]
    assert cwt["monthly_usage"] == pytest.approx(55473.6 / 3)      # 18491.2
    assert cwt["usage_3mo"] == pytest.approx(55473.6)
    assert cwt["on_hand"] == pytest.approx(30000)
    assert cwt["allocated"] == pytest.approx(6000)
    assert cwt["in_transit"] == pytest.approx(0)
    assert cwt["months_of_stock"] == pytest.approx(30000 / (55473.6 / 3))
    assert cwt["requirement_kg"] == pytest.approx(31473.6)
    assert cwt["vendor"] == "Visy Board"
    assert cwt["lead_time_days"] == 45
    assert cwt["grade"] == "CWT140"
    assert cwt["deckle_mm"] == 1400
    assert cwt["uom"] == "KG"
    assert cwt["as_of"] is not None                          # freshness always shown

    rf = rows["RF135-1000"]
    assert rf["monthly_usage"] == pytest.approx(46672.5 / 3)       # 15557.5
    assert rf["requirement_kg"] == pytest.approx(29672.5)
    assert rf["vendor"] == "Changle Numat (CSC)"

    bx186 = rows["BX186-1400"]
    assert bx186["monthly_usage"] == pytest.approx(50190.4 / 3)    # 16730.13
    assert bx186["months_of_stock"] == pytest.approx(60000 / (50190.4 / 3))  # 3.59
    assert bx186["requirement_kg"] == pytest.approx(0.0)     # covered -> no order


# --------------------------------------------------------------------------- #
# Order Page: exact HISTORY figures (trailing 3-month mean)
# --------------------------------------------------------------------------- #
def test_order_page_history_figures(client):
    as_role("VIEWER")
    rows = _rows_by_sku(client.get("/api/planning/order-page").json())

    hp = rows["HP140-1490"]
    assert hp["monthly_usage"] == pytest.approx((21800 + 22600 + 22100) / 3)
    assert hp["requirement_kg"] == pytest.approx(34500)      # 66500+8000-40000
    assert hp["vendor"] == "Changle Numat (CSC)"             # 1.64 beats Visy 1.70
    assert hp["lead_time_days"] == 60                        # chosen vendor's lead

    cwt1950 = rows["CWT140-1950"]
    assert cwt1950["monthly_usage"] == pytest.approx(7900)
    assert cwt1950["months_of_stock"] == pytest.approx(30000 / 7900)   # 3.80
    assert cwt1950["requirement_kg"] == pytest.approx(0.0)   # 23700+2000-30000 < 0

    bx200 = rows["BX200-1950"]
    assert bx200["monthly_usage"] == pytest.approx(5000)
    assert bx200["months_of_stock"] == pytest.approx(0.8)
    assert bx200["requirement_kg"] == pytest.approx(12500)   # 15000+1500-4000


# --------------------------------------------------------------------------- #
# Order Page: per-vendor container plans (worked example)
# --------------------------------------------------------------------------- #
def test_order_page_container_plans(client):
    as_role("VIEWER")
    page = client.get("/api/planning/order-page").json()
    plans = _plans_by_vendor(page)
    assert set(plans) == {"Visy Board", "Changle Numat (CSC)"}

    visy = plans["Visy Board"]
    assert visy["containers"] == 2
    assert visy["total_kg"] == pytest.approx(50000)
    assert len(visy["lines"]) == 1
    vline = visy["lines"][0]
    assert vline["sku"] == "CWT140-1400"
    assert vline["requirement_kg"] == pytest.approx(31473.6)
    assert vline["order_kg"] == pytest.approx(50000)         # slack fills the FCLs

    csc = plans["Changle Numat (CSC)"]
    assert csc["containers"] == 4
    assert csc["total_kg"] == pytest.approx(100000)
    lines = {ln["sku"]: ln for ln in csc["lines"]}
    assert set(lines) == {"HP140-1490", "RF135-1000", "BX200-1950"}
    assert lines["HP140-1490"]["order_kg"] == pytest.approx(57827.5)   # + slack
    assert lines["RF135-1000"]["order_kg"] == pytest.approx(29672.5)   # untouched
    assert lines["BX200-1950"]["order_kg"] == pytest.approx(12500)     # untouched
    assert sum(ln["order_kg"] for ln in csc["lines"]) == pytest.approx(100000)


# --------------------------------------------------------------------------- #
# Order Page: below_cover count + clean skipped_forecasts on the seeded data
# --------------------------------------------------------------------------- #
def test_order_page_below_cover_count(client):
    as_role("VIEWER")
    page = client.get("/api/planning/order-page").json()
    # CWT140-1400 1.62, HP140 1.80, RF135 1.29, BX200 0.8 < 3 months of cover.
    assert page["below_cover"] == 4
    assert page["skipped_forecasts"] == []


# --------------------------------------------------------------------------- #
# Order Page: in-transit from an ISSUED PO nets the requirement + counts as cover
# --------------------------------------------------------------------------- #
def test_in_transit_po_reduces_requirement_and_counts_in_cover(client):
    _issued_paper_po(client, sku="BX200-1950", quantity=25000)   # 1 FCL on order
    as_role("VIEWER")
    page = client.get("/api/planning/order-page").json()
    row = _rows_by_sku(page)["BX200-1950"]
    assert row["in_transit"] == pytest.approx(25000)
    # months = (4000 + 25000) / 5000 = 5.8 — the open PO is counted as cover...
    assert row["months_of_stock"] == pytest.approx(5.8)
    # ...and nets the requirement: 15000 + 1500 - 4000 - 25000 < 0 -> 0.
    assert row["requirement_kg"] == pytest.approx(0.0)
    # BX200 climbs above cover -> only CWT140-1400 / HP140 / RF135 remain below.
    assert page["below_cover"] == 3

    # CSC plan reshapes without BX200: 34500 + 29672.5 = 64172.5 -> 3 FCL = 75000,
    # slack 10827.5 onto HP140 -> order_kg 45327.5.
    csc = _plans_by_vendor(page)["Changle Numat (CSC)"]
    assert csc["containers"] == 3
    assert csc["total_kg"] == pytest.approx(75000)
    lines = {ln["sku"]: ln for ln in csc["lines"]}
    assert set(lines) == {"HP140-1490", "RF135-1000"}
    assert lines["HP140-1490"]["order_kg"] == pytest.approx(45327.5)


# --------------------------------------------------------------------------- #
# Order Page: a forecast whose finished item has no BOM is surfaced, not lost
# --------------------------------------------------------------------------- #
def test_skipped_forecast_when_finished_item_has_no_bom(client, engine):
    with Session(engine) as s:
        s.add(Item(sku="CTN-NOBOM", name="Carton With No BOM",
                   item_type=ItemType.FINISHED, uom="EA"))
        s.commit()
    as_role("OFFICER")
    put = client.put("/api/forecasts", json={"lines": [{
        "customer": "Acme Beverages", "sku": "CTN-NOBOM",
        "period": forward_periods(1)[0], "qty_cartons": 5000}]})
    assert put.status_code == 200, put.text

    page = client.get("/api/planning/order-page").json()
    assert page["skipped_forecasts"] == ["CTN-NOBOM"]        # SOP §9 data integrity
    # The un-explodable forecast changes no paper figure.
    assert _rows_by_sku(page)["CWT140-1400"]["requirement_kg"] == pytest.approx(31473.6)


# --------------------------------------------------------------------------- #
# import-usage: idempotent re-import (upsert refreshes, never duplicates)
# --------------------------------------------------------------------------- #
def test_import_usage_idempotent_reimport(client, engine):
    last_period = trailing_periods(6)[-1]
    with Session(engine) as s:
        assert len(s.exec(select(UsageHistory)).all()) == 36   # 6 SKUs x 6 months
        # Tamper one figure so the re-import provably REFRESHES it.
        item = s.exec(select(Item).where(Item.sku == "BX200-1950")).first()
        row = s.exec(select(UsageHistory).where(
            UsageHistory.item_id == item.id,
            UsageHistory.period == last_period)).first()
        row.quantity = 1.0
        s.add(row)
        s.commit()
        item_id = item.id

    as_role("OFFICER")
    first = client.post("/api/planning/import-usage").json()
    assert first["imported"] == 36
    assert first["skipped"] == 0
    assert first["as_of"]

    second = client.post("/api/planning/import-usage").json()
    assert second["imported"] == 36

    with Session(engine) as s:
        rows = s.exec(select(UsageHistory)).all()
        assert len(rows) == 36                               # upserted, no dupes
        restored = s.exec(select(UsageHistory).where(
            UsageHistory.item_id == item_id,
            UsageHistory.period == last_period)).first()
        assert restored.quantity == pytest.approx(4900)      # tail of BX200 series


def test_import_usage_skips_unknown_sku(client, engine, monkeypatch):
    period = trailing_periods(1)[0]
    monkeypatch.setattr(planning_service.bc, "get_usage_entries", lambda: [
        {"sku": "BX200-1950", "period": period, "quantity": 5150.0},
        {"sku": "NOPE-42", "period": period, "quantity": 99.0},    # not carried
        {"sku": "CWT140-1400", "period": None, "quantity": 5.0},   # no period
    ])
    as_role("OFFICER")
    body = client.post("/api/planning/import-usage").json()
    assert body["imported"] == 1
    assert body["skipped"] == 2


def test_import_usage_forbidden_for_viewer(client):
    as_role("VIEWER")
    assert client.post("/api/planning/import-usage").status_code == 403


# --------------------------------------------------------------------------- #
# suggest-orders: ONE DRAFT source='coverage' req with consolidated quantities
# --------------------------------------------------------------------------- #
def test_suggest_orders_creates_one_draft_coverage_requisition(client, engine):
    as_role("OFFICER")
    before = _req_count(engine)
    r = client.post("/api/planning/suggest-orders", json={"cost_center": "CC-PAPER"})
    assert r.status_code == 200, r.text
    req = r.json()
    assert req["status"] == "DRAFT"
    assert req["number"].startswith("REQ-")
    assert req["cost_center"] == "CC-PAPER"
    assert _req_count(engine) == before + 1                  # exactly ONE req

    # One line per shortage grade/deckle at the CONTAINER-CONSOLIDATED quantity.
    lines = {ln["sku"]: ln for ln in req["lines"]}
    assert set(lines) == {"CWT140-1400", "HP140-1490", "RF135-1000", "BX200-1950"}
    assert lines["CWT140-1400"]["quantity"] == pytest.approx(50000)    # 2 FCL Visy
    assert lines["HP140-1490"]["quantity"] == pytest.approx(57827.5)   # req + slack
    assert lines["RF135-1000"]["quantity"] == pytest.approx(29672.5)
    assert lines["BX200-1950"]["quantity"] == pytest.approx(12500)

    # The consolidation maths rides along with the requisition detail.
    plans = _plans_by_vendor(req)
    assert plans["Visy Board"]["containers"] == 2
    assert plans["Changle Numat (CSC)"]["containers"] == 4

    with Session(engine) as s:
        row = s.get(Requisition, req["id"])
        assert row.source == "coverage"
        rlines = s.exec(select(RequisitionLine).where(
            RequisitionLine.requisition_id == req["id"])).all()
        assert len(rlines) == 4


def test_suggest_orders_records_created_event(client, engine):
    as_role("OFFICER")
    req = client.post("/api/planning/suggest-orders", json={}).json()
    with Session(engine) as s:
        evts = s.exec(select(OrderEvent).where(
            OrderEvent.entity_kind == "REQUISITION",
            OrderEvent.entity_id == req["id"])).all()
    assert len(evts) == 1
    assert evts[0].event_type == "CREATED"
    assert evts[0].to_status == "DRAFT"
    assert evts[0].actor == "officer@golden.com.fj"
    detail = json.loads(evts[0].detail_json)
    assert detail["source"] == "coverage"
    assert detail["line_count"] == 4


def test_suggest_orders_nothing_to_order(client, engine):
    # With no usage and no forecasts every grade has monthly=0 and requirement
    # max(0, allocated - on_hand) = 0 (on_hand exceeds allocated across the demo).
    with Session(engine) as s:
        for row in s.exec(select(UsageHistory)).all():
            s.delete(row)
        for f in s.exec(select(Forecast)).all():
            s.delete(f)
        s.commit()
    as_role("OFFICER")
    before = _req_count(engine)
    r = client.post("/api/planning/suggest-orders", json={})
    assert r.status_code == 200
    assert r.json() == {"created": False, "message": "all grades at or above cover"}
    assert _req_count(engine) == before                      # nothing created


# --------------------------------------------------------------------------- #
# RBAC + auth
# --------------------------------------------------------------------------- #
def test_viewer_can_read_order_page(client):
    as_role("VIEWER")
    assert client.get("/api/planning/order-page").status_code == 200


def test_order_page_requires_auth(client):
    assert client.get("/api/planning/order-page").status_code == 401


def test_viewer_cannot_suggest_orders(client):
    as_role("VIEWER")
    assert client.post("/api/planning/suggest-orders", json={}).status_code == 403


# --------------------------------------------------------------------------- #
# Regression: consolidation survives PO creation (no per-line MOQ inflation)
# --------------------------------------------------------------------------- #
def test_coverage_requisition_becomes_whole_fcl_pos(client, engine):
    """The SOP's container rule is ORDER-level: grades/deckles combine to fill
    whole FCLs per vendor. Phase 3 PO creation bumps each line to the vendor
    MOQ (max(qty, moq)), so paper vendor_prices must NOT carry a per-line 25t
    moq — otherwise the CSC order here would inflate from 100000 (4 FCL) to
    112500 kg (4.5 FCL) and un-do the consolidation. Locks the whole path:
    suggest-orders -> approval lifecycle -> vendor-grouped POs."""
    as_role("OFFICER")
    req = client.post("/api/planning/suggest-orders", json={}).json()

    client.post(f"/api/requisitions/{req['id']}/submit")
    as_role("ADMIN")                     # unlimited approval tier for a paper req
    r = client.post(f"/api/requisitions/{req['id']}/approve")
    assert r.status_code == 200, r.text

    r = client.post(f"/api/requisitions/{req['id']}/create-po")
    assert r.status_code == 201, r.text
    pos = r.json()
    totals = {
        po["vendor"]["name"]: sum(ln["quantity"] for ln in po["lines"])
        for po in pos
    }
    # Per-vendor totals stay whole 25t containers...
    assert totals["Visy Board"] == pytest.approx(2 * KG_PER_FCL)
    assert totals["Changle Numat (CSC)"] == pytest.approx(4 * KG_PER_FCL)
    # ...and every line keeps its consolidated quantity (nothing MOQ-bumped).
    csc = next(po for po in pos if po["vendor"]["name"] == "Changle Numat (CSC)")
    by_sku = {ln["sku"]: ln["quantity"] for ln in csc["lines"]}
    assert by_sku["BX200-1950"] == pytest.approx(12500)
    assert by_sku["HP140-1490"] == pytest.approx(57827.5)
    assert by_sku["RF135-1000"] == pytest.approx(29672.5)
