"""BOM CRUD — the board-grade spec (SOP step 2's master data).

PUT/DELETE /api/items/{sku}/bom maintain the APP-owned top "kit" bill (CLAUDE.md
§2): the carton -> KG-per-grade spec the forecast explosion runs through. Upserts
are VERSIONED (the old ACTIVE header goes OBSOLETE, never deleted), gated to
OFFICER/ADMIN, refused for bills mirrored from KIWIPLAN/ACCURA (409), and the
candidate structure is exploded before commit so a cycle can never land (409).

All numbers are computed against the SEEDED demo data (conftest refresh_all ->
fakes.py):

Seeded CTN-FIJIWATER-1L v1 bill (owner APP, yield 1.0):
  CWT140-1400 0.42 @ 4%   RF135-1000 0.35 @ 5%
  BX186-1400  0.38 @ 4%   GLUE-STARCH 0.03 @ 0%

Re-spec worked example — CWT140-1400 qty_per 0.42 -> 0.50 (scrap 0.04 kept):
  explode 1000 cartons: CWT140-1400 gross = 1000*0.50*1.04 = 520
                        RF135-1000  gross = 1000*0.35*1.05 = 367.5 (unchanged)
  Order Page (demo forecast 42000+45000+40000 = 127000 cartons over the current
  3-month window):
    CWT140-1400 window kg = 127000*0.50*1.04 = 66040
    monthly_forecast      = 66040/3          = 22013.33
      > history (17200+18900+18000)/3 = 18033.33  -> basis stays FORECAST
    requirement = 66040 + 6000(alloc) - 30000(on_hand) - 0(in transit) = 42040

Cycle fixture note: the SEEDED CTN bill already contains GLUE-STARCH, so putting
CTN straight into a GLUE-STARCH bill is itself a cycle (GLUE -> CTN -> GLUE) and
must 409 with no write; the "PUT then reverse-PUT" cycle test first re-specs CTN
WITHOUT glue so the forward edge is legal before the reverse edge is attempted.
"""
import json

import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.gateway.models import BomHeader, BomLine, BomOwner, Item, OrderEvent
from app.main import app

LIMITS = {
    "ADMIN": None,
    "APPROVER": 50000.0,
    "OFFICER": 5000.0,
    "REQUESTER": 0.0,
    "VIEWER": 0.0,
}

CTN = "CTN-FIJIWATER-1L"

# The seeded v1 spec, as a PUT body would express it.
V1_LINES = [
    {"sku": "CWT140-1400", "qty_per": 0.42, "scrap_pct": 0.04},
    {"sku": "RF135-1000", "qty_per": 0.35, "scrap_pct": 0.05},
    {"sku": "BX186-1400", "qty_per": 0.38, "scrap_pct": 0.04},
    {"sku": "GLUE-STARCH", "qty_per": 0.03, "scrap_pct": 0.0},
]

# The re-spec: CWT140-1400 0.42 -> 0.50, everything else unchanged.
RESPEC_LINES = [
    {"sku": "CWT140-1400", "qty_per": 0.50, "scrap_pct": 0.04},
    {"sku": "RF135-1000", "qty_per": 0.35, "scrap_pct": 0.05},
    {"sku": "BX186-1400", "qty_per": 0.38, "scrap_pct": 0.04},
    {"sku": "GLUE-STARCH", "qty_per": 0.03, "scrap_pct": 0.0},
]


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
# Helpers
# --------------------------------------------------------------------------- #
def _put_bom(client, sku, lines, yield_qty=1.0):
    return client.put(f"/api/items/{sku}/bom",
                      json={"yield_qty": yield_qty, "lines": lines})


def _by_sku(rows):
    return {r["sku"]: r for r in rows}


def _header_rows(engine, sku):
    """[(version, status, owner, line_count)] for the parent, ordered by version."""
    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == sku)).first()
        headers = s.exec(
            select(BomHeader).where(BomHeader.parent_item_id == item.id)
            .order_by(BomHeader.version)).all()
        out = []
        for h in headers:
            lines = s.exec(select(BomLine).where(
                BomLine.bom_header_id == h.id)).all()
            out.append((h.version, h.status, h.owner.value, len(lines)))
        return out


def _bom_events(engine, sku, event_type=None):
    """[(event_type, from_status, to_status, actor, detail)] for entity_kind BOM."""
    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == sku)).first()
        q = select(OrderEvent).where(
            OrderEvent.entity_kind == "BOM", OrderEvent.entity_id == item.id)
        if event_type:
            q = q.where(OrderEvent.event_type == event_type)
        return [
            (e.event_type, e.from_status, e.to_status, e.actor,
             json.loads(e.detail_json) if e.detail_json else None)
            for e in s.exec(q.order_by(OrderEvent.id)).all()
        ]


def _flip_owner(engine, sku, owner):
    """Make the ACTIVE header look mirrored from a production system."""
    with Session(engine) as s:
        item = s.exec(select(Item).where(Item.sku == sku)).first()
        header = s.exec(select(BomHeader).where(
            BomHeader.parent_item_id == item.id,
            BomHeader.status == "ACTIVE")).first()
        header.owner = owner
        s.add(header)
        s.commit()


# --------------------------------------------------------------------------- #
# Versioned upsert: PUT makes v2 ACTIVE, v1 OBSOLETE (never deleted)
# --------------------------------------------------------------------------- #
def test_put_creates_v2_and_marks_v1_obsolete(client, engine):
    as_role("OFFICER")
    r = _put_bom(client, CTN, RESPEC_LINES)
    assert r.status_code == 200, r.text

    rows = _header_rows(engine, CTN)
    assert rows == [
        (1, "OBSOLETE", "APP", 4),      # retired, lines kept for the audit trail
        (2, "ACTIVE", "APP", 4),        # the new spec
    ]


def test_put_response_and_get_tree_reflect_new_lines(client):
    as_role("OFFICER")
    put_tree = _put_bom(client, CTN, RESPEC_LINES).json()
    # The PUT response is the fresh tree...
    comps = _by_sku(put_tree["components"])
    assert comps["CWT140-1400"]["qty_per"] == pytest.approx(0.50)
    assert comps["CWT140-1400"]["scrap_pct"] == pytest.approx(0.04)
    assert comps["CWT140-1400"]["owner"] == "APP"

    # ...and GET serves the SAME new spec (the v2 header, not the retired v1).
    tree = client.get(f"/api/items/{CTN}/bom").json()
    assert tree["sku"] == CTN
    # Line order preserved (line_no follows the body order).
    assert [c["sku"] for c in tree["components"]] == [ln["sku"] for ln in RESPEC_LINES]
    got = _by_sku(tree["components"])
    assert got["CWT140-1400"]["qty_per"] == pytest.approx(0.50)
    assert got["RF135-1000"]["qty_per"] == pytest.approx(0.35)
    assert got["RF135-1000"]["scrap_pct"] == pytest.approx(0.05)
    assert got["GLUE-STARCH"]["qty_per"] == pytest.approx(0.03)


def test_second_put_creates_v3(client, engine):
    as_role("OFFICER")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 200
    # Re-spec again: drop the glue line entirely (3-line bill).
    assert _put_bom(client, CTN, RESPEC_LINES[:3]).status_code == 200

    rows = _header_rows(engine, CTN)
    assert rows == [
        (1, "OBSOLETE", "APP", 4),
        (2, "OBSOLETE", "APP", 4),
        (3, "ACTIVE", "APP", 3),
    ]
    tree = client.get(f"/api/items/{CTN}/bom").json()
    assert set(_by_sku(tree["components"])) == {
        "CWT140-1400", "RF135-1000", "BX186-1400"}


# --------------------------------------------------------------------------- #
# The explosion + Order Page consume the NEW spec (0.42 -> 0.50 worked example)
# --------------------------------------------------------------------------- #
def test_respec_drives_explosion(client):
    as_role("OFFICER")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 200

    as_role("VIEWER")
    body = client.post("/api/bom/explode", json={
        "lines": [{"sku": CTN, "qty": 1000}]}).json()
    gross = _by_sku(body["gross"])
    # New spec: 1000*0.50*1.04 = 520 (was 1000*0.42*1.04 = 436.8).
    assert gross["CWT140-1400"]["qty"] == pytest.approx(520)
    # Untouched lines still explode off their old figures.
    assert gross["RF135-1000"]["qty"] == pytest.approx(367.5)   # 1000*0.35*1.05
    assert gross["GLUE-STARCH"]["qty"] == pytest.approx(30)     # 1000*0.03


def test_respec_drives_order_page_forecast(client):
    as_role("OFFICER")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 200

    as_role("VIEWER")
    rows = _by_sku(client.get("/api/planning/order-page").json()["rows"])
    cwt = rows["CWT140-1400"]
    # 127000 cartons * 0.50 * 1.04 = 66040 kg over the window -> /3 = 22013.33.
    assert cwt["monthly_forecast"] == pytest.approx(127000 * 0.50 * 1.04 / 3)
    # 22013.33 > history 18033.33 -> the forecast basis still wins...
    assert cwt["basis"] == "FORECAST"
    assert cwt["monthly_usage"] == pytest.approx(22013.3333, rel=1e-4)
    # ...and the SOP order rule runs off it: 66040 + 6000 - 30000 = 42040.
    assert cwt["requirement_kg"] == pytest.approx(42040)


# --------------------------------------------------------------------------- #
# RBAC + auth: OFFICER/ADMIN mutate; VIEWER 403; no session 401
# --------------------------------------------------------------------------- #
def test_put_forbidden_for_viewer(client):
    as_role("VIEWER")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 403


def test_delete_forbidden_for_viewer(client):
    as_role("VIEWER")
    assert client.delete(f"/api/items/{CTN}/bom").status_code == 403


def test_put_requires_auth(client):
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 401


def test_delete_requires_auth(client):
    assert client.delete(f"/api/items/{CTN}/bom").status_code == 401


def test_admin_can_put(client, engine):
    as_role("ADMIN")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 200
    assert _header_rows(engine, CTN)[-1] == (2, "ACTIVE", "APP", 4)


# --------------------------------------------------------------------------- #
# Validation: unknown SKUs 404; bad structure 400; bad values 422
# --------------------------------------------------------------------------- #
def test_put_unknown_parent_sku_404(client):
    as_role("OFFICER")
    r = _put_bom(client, "NOPE-999", RESPEC_LINES)
    assert r.status_code == 404


def test_put_unknown_component_sku_404(client):
    as_role("OFFICER")
    r = _put_bom(client, CTN, [{"sku": "NOPE-999", "qty_per": 0.5}])
    assert r.status_code == 404


def test_put_duplicate_component_400(client):
    as_role("OFFICER")
    r = _put_bom(client, CTN, [
        {"sku": "CWT140-1400", "qty_per": 0.5},
        {"sku": "CWT140-1400", "qty_per": 0.4},
    ])
    assert r.status_code == 400
    assert "duplicate" in r.json()["detail"].lower()


def test_put_parent_in_own_bill_400(client):
    as_role("OFFICER")
    r = _put_bom(client, CTN, [{"sku": CTN, "qty_per": 1.0}])
    assert r.status_code == 400
    assert "own parent" in r.json()["detail"].lower()


def test_put_qty_per_zero_or_negative_422(client):
    as_role("OFFICER")
    assert _put_bom(client, CTN, [
        {"sku": "CWT140-1400", "qty_per": 0}]).status_code == 422
    assert _put_bom(client, CTN, [
        {"sku": "CWT140-1400", "qty_per": -0.5}]).status_code == 422


def test_put_scrap_pct_over_one_422(client):
    as_role("OFFICER")
    r = _put_bom(client, CTN, [
        {"sku": "CWT140-1400", "qty_per": 0.5, "scrap_pct": 1.5}])
    assert r.status_code == 422


def test_put_empty_lines_422(client):
    as_role("OFFICER")
    assert _put_bom(client, CTN, []).status_code == 422


def test_rejected_put_writes_nothing(client, engine):
    # A 400 must not half-commit: the seeded v1 stays the only (ACTIVE) header.
    as_role("OFFICER")
    _put_bom(client, CTN, [{"sku": CTN, "qty_per": 1.0}])
    assert _header_rows(engine, CTN) == [(1, "ACTIVE", "APP", 4)]
    assert _bom_events(engine, CTN) == []


# --------------------------------------------------------------------------- #
# Mirrored guard: KIWIPLAN/ACCURA bills are production truth -> read-only (409)
# --------------------------------------------------------------------------- #
def test_put_mirrored_bill_409_names_owner(client, engine):
    _flip_owner(engine, CTN, BomOwner.KIWIPLAN)
    as_role("OFFICER")
    r = _put_bom(client, CTN, RESPEC_LINES)
    assert r.status_code == 409
    assert "KIWIPLAN" in r.json()["detail"]


def test_delete_mirrored_bill_409_names_owner(client, engine):
    _flip_owner(engine, CTN, BomOwner.KIWIPLAN)
    as_role("OFFICER")
    r = client.delete(f"/api/items/{CTN}/bom")
    assert r.status_code == 409
    assert "KIWIPLAN" in r.json()["detail"]
    # The mirrored header is untouched.
    assert _header_rows(engine, CTN) == [(1, "ACTIVE", "KIWIPLAN", 4)]


# --------------------------------------------------------------------------- #
# Cycle guard: the CANDIDATE structure is exploded before anything commits
# --------------------------------------------------------------------------- #
def test_put_cycle_409(client, engine):
    as_role("OFFICER")
    # 1) Re-spec CTN WITHOUT glue so a GLUE-STARCH -> CTN edge is legal.
    assert _put_bom(client, CTN, RESPEC_LINES[:3]).status_code == 200
    # 2) Put a bill on GLUE-STARCH containing CTN — allowed (no cycle yet).
    r = _put_bom(client, "GLUE-STARCH", [{"sku": CTN, "qty_per": 1.0}])
    assert r.status_code == 200, r.text
    # A first-ever bill lands as v1 with no prior version in the event.
    assert _bom_events(engine, "GLUE-STARCH") == [
        ("BOM_UPDATED", None, "v1", "officer@golden.com.fj",
         {"sku": "GLUE-STARCH", "version": 1, "yield_qty": 1.0,
          "lines": [{"sku": CTN, "qty_per": 1.0, "scrap_pct": 0.0}]}),
    ]
    # 3) Closing the loop (CTN -> GLUE-STARCH -> CTN) must 409...
    r = _put_bom(client, CTN, RESPEC_LINES)          # 4 lines incl. GLUE-STARCH
    assert r.status_code == 409
    assert "cycle" in r.json()["detail"].lower()
    # ...and leave CTN at v2 ACTIVE (no v3 header, no extra event).
    assert _header_rows(engine, CTN) == [
        (1, "OBSOLETE", "APP", 4), (2, "ACTIVE", "APP", 3)]
    assert len(_bom_events(engine, CTN, "BOM_UPDATED")) == 1


def test_put_direct_cycle_on_seeded_bill_409_no_write(client, engine):
    # The SEEDED CTN bill already contains GLUE-STARCH, so GLUE -> CTN is a
    # cycle on the spot: rejected up front, and GLUE-STARCH stays a leaf.
    as_role("OFFICER")
    r = _put_bom(client, "GLUE-STARCH", [{"sku": CTN, "qty_per": 1.0}])
    assert r.status_code == 409
    assert "cycle" in r.json()["detail"].lower()
    assert _header_rows(engine, "GLUE-STARCH") == []
    assert client.get("/api/items/GLUE-STARCH/bom").json() is None


# --------------------------------------------------------------------------- #
# DELETE retires: 204, tree null, ex-parent becomes its own purchased leaf
# --------------------------------------------------------------------------- #
def test_delete_retires_bill(client, engine):
    as_role("OFFICER")
    r = client.delete(f"/api/items/{CTN}/bom")
    assert r.status_code == 204

    # Retired, never deleted: v1 is OBSOLETE with its 4 lines intact.
    assert _header_rows(engine, CTN) == [(1, "OBSOLETE", "APP", 4)]

    # The tree is gone...
    assert client.get(f"/api/items/{CTN}/bom").json() is None

    # ...and the ex-parent now explodes as its OWN leaf (recursion stops).
    body = client.post("/api/bom/explode", json={
        "lines": [{"sku": CTN, "qty": 100}]}).json()
    gross = _by_sku(body["gross"])
    assert set(gross) == {CTN}
    assert gross[CTN]["qty"] == pytest.approx(100)


def test_delete_twice_404(client):
    as_role("OFFICER")
    assert client.delete(f"/api/items/{CTN}/bom").status_code == 204
    assert client.delete(f"/api/items/{CTN}/bom").status_code == 404


def test_delete_unknown_sku_404(client):
    as_role("OFFICER")
    assert client.delete("/api/items/NOPE-999/bom").status_code == 404


def test_delete_leaf_without_bill_404(client):
    as_role("OFFICER")
    assert client.delete("/api/items/CWT140-1400/bom").status_code == 404


# --------------------------------------------------------------------------- #
# Audit: BOM_UPDATED carries v1 -> v2 (-> v3); BOM_RETIRED closes the trail
# --------------------------------------------------------------------------- #
def test_put_records_bom_updated_events_with_versions(client, engine):
    as_role("OFFICER")
    assert _put_bom(client, CTN, RESPEC_LINES).status_code == 200
    assert _put_bom(client, CTN, RESPEC_LINES[:3]).status_code == 200

    evts = _bom_events(engine, CTN, "BOM_UPDATED")
    assert [(e[1], e[2]) for e in evts] == [("v1", "v2"), ("v2", "v3")]
    assert all(e[3] == "officer@golden.com.fj" for e in evts)
    # The event snapshot carries the full spec an audit would replay.
    detail = evts[0][4]
    assert detail["sku"] == CTN
    assert detail["version"] == 2
    assert detail["lines"][0] == {
        "sku": "CWT140-1400", "qty_per": 0.50, "scrap_pct": 0.04}


def test_delete_records_bom_retired_event(client, engine):
    as_role("OFFICER")
    assert client.delete(f"/api/items/{CTN}/bom").status_code == 204

    evts = _bom_events(engine, CTN, "BOM_RETIRED")
    assert len(evts) == 1
    event_type, from_status, to_status, actor, detail = evts[0]
    assert from_status == "v1"
    assert to_status is None
    assert actor == "officer@golden.com.fj"
    assert detail == {"sku": CTN, "version": 1}
