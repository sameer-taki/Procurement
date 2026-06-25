from app.auth.deps import CurrentUser, get_current_user
from app.main import app


def test_search_returns_catalog(admin_client):
    body = admin_client.get("/api/stock").json()
    skus = {r["sku"] for r in body["results"]}
    assert "BOARD-200K" in skus
    assert any(s["system"] == "KIWIPLAN" for s in body["systems"])


def test_search_query_filters(admin_client):
    body = admin_client.get("/api/stock", params={"q": "BOARD"}).json()
    assert body["results"]
    assert all("BOARD" in r["sku"] for r in body["results"])


def test_unified_view_aggregates_locations(admin_client):
    v = admin_client.get("/api/stock/BOARD-200K").json()
    # two Kiwiplan locations: 12450 + 3100 on hand
    assert v["totals"]["on_hand"] == 15550
    assert v["totals"]["available"] == 15550 - 5100 + 6000
    ki = next(s for s in v["by_system"] if s["system"] == "KIWIPLAN")
    assert len(ki["rows"]) == 2
    assert ki["mode"] == "demo"
    assert v["price"]["currency"] == "FJD"
    assert v["as_of"] is not None


def test_unknown_sku_404(admin_client):
    assert admin_client.get("/api/stock/NOPE").status_code == 404


def test_refresh_single(admin_client):
    r = admin_client.post("/api/stock/INK-FLEXO-CYAN/refresh")
    assert r.status_code == 200
    assert r.json()["below_reorder"] is True       # 95-40+0 = 55 < 200


def test_dashboard_counts(admin_client):
    d = admin_client.get("/api/dashboard").json()
    assert d["counts"]["items"] == 12
    assert d["counts"]["below_reorder"] >= 1
    assert any(x["sku"] == "WIRE-STITCH" for x in d["low_stock"])


def test_stock_requires_auth(client):
    assert client.get("/api/stock").status_code == 401


def test_refresh_all_forbidden_for_viewer(client):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id="v1", email="v@x", name="V", role_code="VIEWER", approval_limit=0.0
    )
    try:
        assert client.post("/api/stock-refresh-all").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
