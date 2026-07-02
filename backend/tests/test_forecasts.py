"""Customer forecasts (SOP step 1) — capture + review of the demand input.

Sales/CS submit CARTONS per (customer, finished item, calendar month); a
resubmitted figure REPLACES the previous one (statement of current truth, not a
ledger), and one bad line in a batch writes nothing (atomic upsert).

Seeded demo forecast (conftest refresh_all -> seed_forecasts, fakes.forecasts()):
  'Fiji Water' x CTN-FIJIWATER-1L over the current + next 2 months:
    period[0] 42000  ·  period[1] 45000  ·  period[2] 40000   (127000 total)
Periods are computed with the same forward_periods helper the app uses, so the
tests never go stale at a month boundary.
"""
import pytest
from sqlmodel import Session, select

from app.auth.deps import CurrentUser, get_current_user
from app.domain.planning import forward_periods
from app.gateway.models import Forecast
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


def _line(customer, sku, period, qty):
    return {"customer": customer, "sku": sku, "period": period, "qty_cartons": qty}


def _forecast_count(engine):
    with Session(engine) as s:
        return len(s.exec(select(Forecast)).all())


# --------------------------------------------------------------------------- #
# Seeded rows list with the full shape
# --------------------------------------------------------------------------- #
def test_seeded_forecast_listing(client):
    as_role("VIEWER")
    rows = client.get("/api/forecasts",
                      params={"customer": "Fiji Water"}).json()
    assert len(rows) == 3
    by_period = {r["period"]: r for r in rows}
    p = forward_periods(3)
    assert by_period[p[0]]["qty_cartons"] == pytest.approx(42000)
    assert by_period[p[1]]["qty_cartons"] == pytest.approx(45000)
    assert by_period[p[2]]["qty_cartons"] == pytest.approx(40000)
    row = by_period[p[0]]
    assert row["id"]
    assert row["sku"] == "CTN-FIJIWATER-1L"
    assert row["name"] == "Fiji Water 1L Shipper Carton"
    assert row["updated_by"] == "demo-seed"
    assert row["updated_at"]


# --------------------------------------------------------------------------- #
# Upsert: create then REPLACE on the same (customer, sku, period) key
# --------------------------------------------------------------------------- #
def test_upsert_creates_then_replaces(client):
    p0 = forward_periods(1)[0]
    as_role("OFFICER")
    r1 = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p0, 100)]})
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"written": 1}

    rows = client.get("/api/forecasts", params={"customer": "Acme Beverages"}).json()
    assert len(rows) == 1
    assert rows[0]["qty_cartons"] == pytest.approx(100)
    first_id = rows[0]["id"]

    # Same key again: the figure is REPLACED, not appended.
    r2 = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p0, 250)]})
    assert r2.json() == {"written": 1}
    rows = client.get("/api/forecasts", params={"customer": "Acme Beverages"}).json()
    assert len(rows) == 1                                    # still one row
    assert rows[0]["id"] == first_id                         # updated in place
    assert rows[0]["qty_cartons"] == pytest.approx(250)
    assert rows[0]["updated_by"] == "officer@golden.com.fj"


def test_upsert_written_counts_the_batch(client, engine):
    p = forward_periods(2)
    before = _forecast_count(engine)
    as_role("OFFICER")
    r = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p[0], 500),
        _line("Acme Beverages", "BOX-RSC-A", p[1], 600),
    ]})
    assert r.json() == {"written": 2}
    assert _forecast_count(engine) == before + 2


def test_upsert_zero_is_an_explicit_no_demand(client):
    # 0 cartons is meaningful (an overwrite to 'no demand'), never rejected.
    p0 = forward_periods(1)[0]
    as_role("OFFICER")
    r = client.put("/api/forecasts", json={"lines": [
        _line("Fiji Water", "CTN-FIJIWATER-1L", p0, 0)]})
    assert r.status_code == 200
    rows = client.get("/api/forecasts",
                      params={"customer": "Fiji Water", "period": p0}).json()
    assert len(rows) == 1
    assert rows[0]["qty_cartons"] == pytest.approx(0)


# --------------------------------------------------------------------------- #
# List filters: customer / period / sku (unknown sku -> empty, not an error)
# --------------------------------------------------------------------------- #
def test_list_filters(client):
    p = forward_periods(3)
    as_role("OFFICER")
    client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p[0], 100),
        _line("Acme Beverages", "LABEL-1L-RANGE", p[1], 900),
    ]})

    assert len(client.get("/api/forecasts",
                          params={"customer": "Fiji Water"}).json()) == 3
    assert len(client.get("/api/forecasts",
                          params={"customer": "Acme Beverages"}).json()) == 2

    in_p0 = client.get("/api/forecasts", params={"period": p[0]}).json()
    assert {r["period"] for r in in_p0} == {p[0]}
    assert {r["customer"] for r in in_p0} == {"Fiji Water", "Acme Beverages"}

    by_sku = client.get("/api/forecasts",
                        params={"sku": "CTN-FIJIWATER-1L"}).json()
    assert len(by_sku) == 3
    assert {r["sku"] for r in by_sku} == {"CTN-FIJIWATER-1L"}

    combo = client.get("/api/forecasts", params={
        "customer": "Acme Beverages", "sku": "BOX-RSC-A"}).json()
    assert len(combo) == 1
    assert combo[0]["qty_cartons"] == pytest.approx(100)

    assert client.get("/api/forecasts", params={"sku": "NOPE-999"}).json() == []


# --------------------------------------------------------------------------- #
# Validation: bad period 400 · unknown sku 404 (atomic) · negative qty 422
# --------------------------------------------------------------------------- #
def test_bad_period_is_400(client):
    as_role("OFFICER")
    for bad in ("2026-13", "202607", "07-2026"):
        r = client.put("/api/forecasts", json={"lines": [
            _line("Acme Beverages", "BOX-RSC-A", bad, 10)]})
        assert r.status_code == 400, f"{bad}: {r.status_code}"


def test_unknown_sku_is_404_and_atomic(client, engine):
    # The first (valid) line must NOT be written when a later line fails.
    p0 = forward_periods(1)[0]
    before = _forecast_count(engine)
    as_role("OFFICER")
    r = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p0, 10),
        _line("Acme Beverages", "NOPE-999", p0, 20),
    ]})
    assert r.status_code == 404
    assert _forecast_count(engine) == before                 # nothing written
    assert client.get("/api/forecasts",
                      params={"customer": "Acme Beverages"}).json() == []


def test_negative_qty_is_422(client):
    as_role("OFFICER")
    r = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", forward_periods(1)[0], -1)]})
    assert r.status_code == 422


def test_empty_batch_is_422(client):
    as_role("OFFICER")
    assert client.put("/api/forecasts", json={"lines": []}).status_code == 422


# --------------------------------------------------------------------------- #
# Delete: 204 then the list shrinks; unknown id 404
# --------------------------------------------------------------------------- #
def test_delete_removes_the_row(client):
    p0 = forward_periods(1)[0]
    as_role("OFFICER")
    client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", p0, 100)]})
    rows = client.get("/api/forecasts", params={"customer": "Acme Beverages"}).json()
    assert len(rows) == 1

    r = client.delete(f"/api/forecasts/{rows[0]['id']}")
    assert r.status_code == 204
    assert client.get("/api/forecasts",
                      params={"customer": "Acme Beverages"}).json() == []


def test_delete_unknown_is_404(client):
    as_role("OFFICER")
    assert client.delete("/api/forecasts/nope").status_code == 404


# --------------------------------------------------------------------------- #
# RBAC: VIEWER reads but never writes; unauthenticated is 401
# --------------------------------------------------------------------------- #
def test_viewer_can_list(client):
    as_role("VIEWER")
    assert client.get("/api/forecasts").status_code == 200


def test_viewer_cannot_upsert(client):
    as_role("VIEWER")
    r = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", forward_periods(1)[0], 10)]})
    assert r.status_code == 403


def test_viewer_cannot_delete(client):
    as_role("OFFICER")
    client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", forward_periods(1)[0], 10)]})
    rows = client.get("/api/forecasts", params={"customer": "Acme Beverages"}).json()
    as_role("VIEWER")
    assert client.delete(f"/api/forecasts/{rows[0]['id']}").status_code == 403


def test_unauthenticated_is_401(client):
    assert client.get("/api/forecasts").status_code == 401
    r = client.put("/api/forecasts", json={"lines": [
        _line("Acme Beverages", "BOX-RSC-A", forward_periods(1)[0], 10)]})
    assert r.status_code == 401


def test_period_with_trailing_newline_rejected(client):
    """'2026-07\\n' must fail validation: it would pass a $-anchored regex but
    never match the planning window's exact-string query, so the forecast would
    silently vanish from the Order Page."""
    as_role("OFFICER")
    r = client.put("/api/forecasts", json={"lines": [{
        "customer": "Fiji Water", "sku": "CTN-FIJIWATER-1L",
        "period": "2026-07\n", "qty_cartons": 100}]})
    assert r.status_code == 400
