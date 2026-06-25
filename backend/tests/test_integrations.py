"""Cover the pure parts of the live adapter paths (no network/driver needed).

The transport/connect themselves run only on the Golden host against the real
systems; here we verify response parsing + column mapping + the demo/live switch.
"""
from app.config import settings
from app.gateway import _odbc, bc


def test_odbc_map_rows_by_column_name():
    cols = ["LOCATION", "ON_HAND", "ALLOCATED", "ON_ORDER"]
    rows = [["Suva", 100, 10, 5], ["Lautoka", None, None, None]]
    out = _odbc.map_rows(cols, rows)
    assert out[0] == {"location": "Suva", "on_hand": 100.0, "allocated": 10.0, "on_order": 5.0}
    # NULLs coerce to 0; column order independence handled by name lookup
    assert out[1] == {"location": "Lautoka", "on_hand": 0.0, "allocated": 0.0, "on_order": 0.0}


def test_bc_map_item_uses_standard_fields():
    mapped = bc.BCAdapter._map_item({
        "No": "BC-1001", "Description": "Kraft Linerboard",
        "Base_Unit_of_Measure": "KG", "Unit_Price": 1.95,
    })
    assert mapped["sku"] == "BC-1001"
    assert mapped["bc_item_no"] == "BC-1001"
    assert mapped["name"] == "Kraft Linerboard"
    assert mapped["uom"] == "KG"
    assert mapped["sales_price"] == 1.95


def test_bc_list_items_live_parses_value_and_paginates(monkeypatch):
    pages = {
        "u1": {"value": [{"No": "A", "Description": "Item A", "Unit_Price": 2.0}],
               "@odata.nextLink": "u2"},
        "u2": {"value": [{"No": "B", "Description": "Item B", "Unit_Price": 3.0}]},
    }
    adapter = BC_live(monkeypatch, pages, start_url="u1")
    items = adapter.list_items()
    assert [i["sku"] for i in items] == ["A", "B"]
    assert items[1]["sales_price"] == 3.0


def BC_live(monkeypatch, pages, start_url):
    """Build a BCAdapter forced into live mode with _get stubbed to walk `pages`."""
    monkeypatch.setattr(settings, "use_fake_adapters", False)
    monkeypatch.setattr(settings, "bc_base_url", "http://bc")
    monkeypatch.setattr(settings, "bc_username", "u")
    monkeypatch.setattr(settings, "bc_password", "p")
    adapter = bc.BCAdapter()
    assert adapter.use_fakes is False
    monkeypatch.setattr(adapter, "_company_url", lambda: "")
    monkeypatch.setattr(settings, "bc_items_entity", start_url)  # first url == "u1"
    monkeypatch.setattr(adapter, "_get", lambda url, params=None: pages[url.lstrip("/")])
    return adapter
