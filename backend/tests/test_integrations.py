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


def test_bc_list_items_live_keyset_pages_the_master(monkeypatch):
    """Item master is read with KEYSET paging (No gt '<last>' + $orderby) because
    this tenant ignores $skip and its server paging dumps the whole table at once.
    With page size 2 and 3 rows: page 1 (no filter) -> A,B; page 2 (No gt 'B') ->
    C (short) -> stop."""
    import re

    all_rows = [
        {"No": "A", "Description": "Item A", "Unit_Price": 2.0},
        {"No": "B", "Description": "Item B", "Unit_Price": 3.0},
        {"No": "C", "Description": "Item C", "Unit_Price": 4.0},
    ]
    monkeypatch.setattr(settings, "use_fake_adapters", False)
    monkeypatch.setattr(settings, "bc_base_url", "http://bc")
    monkeypatch.setattr(settings, "bc_username", "u")
    monkeypatch.setattr(settings, "bc_password", "p")
    monkeypatch.setattr(settings, "bc_page_size", 2)      # force >1 page
    adapter = bc.BCAdapter()
    assert adapter.use_fakes is False
    monkeypatch.setattr(adapter, "_company_url", lambda: "")

    filters = []

    def fake_get(url, params=None, session=None):
        p = params or {}
        top = int(p.get("$top", len(all_rows)))
        flt = p.get("$filter")
        filters.append(flt)
        rows = all_rows
        if flt:
            gt = re.search(r"No gt '(.*)'", flt).group(1)
            rows = [r for r in all_rows if r["No"] > gt]
        return {"value": rows[:top]}

    monkeypatch.setattr(adapter, "_get", fake_get)
    items = adapter.list_items()
    assert [i["sku"] for i in items] == ["A", "B", "C"]
    assert items[2]["sales_price"] == 4.0
    assert filters == [None, "No gt 'B'"]                 # keyset advanced by last No


def test_bc_usage_entries_live_windows_filter_and_signed_netting(monkeypatch):
    """The live usage read (verified against GML's BC140):
    * the ledger is read ONE MONTH PER REQUEST (`ge <start> and lt <next>`),
      never as one unbounded walk — this tenant's server paging can dump an
      entire table in a single response;
    * every window's $filter carries every configured Entry_Type (Kiwiplan
      usage posts as 'Negative Adjmt.'; 'Consumption' kept for a later switch);
    * quantities are netted SIGNED per (item, month) then flipped, so a
      reversal (+2000) offsets its posting instead of adding to usage;
    * a month whose corrections outweigh usage clamps to zero."""
    import re

    LEDGER = [
        {"Item_No": "WTL175", "Posting_Date": "2026-06-05", "Quantity": -2444},
        {"Item_No": "WTL175", "Posting_Date": "2026-06-09", "Quantity": -2440},
        {"Item_No": "WTL175", "Posting_Date": "2026-06-20", "Quantity": 2000},
        {"Item_No": "BX186", "Posting_Date": "2026-05-02", "Quantity": -459},
        {"Item_No": "BX186", "Posting_Date": "2026-04-01", "Quantity": 500},
    ]
    filters: list[str] = []

    def _get(url, params=None, session=None):
        flt = (params or {}).get("$filter", "")
        filters.append(flt)
        m = re.search(r"Posting_Date ge (\S+) and Posting_Date lt (\S+)", flt)
        assert m, f"every request must be month-windowed, got: {flt}"
        start, end = m.group(1), m.group(2)
        return {"value": [r for r in LEDGER if start <= r["Posting_Date"] < end]}

    monkeypatch.setattr(settings, "use_fake_adapters", False)
    monkeypatch.setattr(settings, "bc_base_url", "http://bc")
    monkeypatch.setattr(settings, "bc_username", "u")
    monkeypatch.setattr(settings, "bc_password", "p")
    # Pin the trailing window so the fixture ledger dates stay inside it forever
    # (get_usage_entries imports trailing_periods from gateway.planning).
    from app.gateway import planning as gw_planning
    monkeypatch.setattr(
        gw_planning, "trailing_periods",
        lambda n, today=None: [f"2026-{m:02d}" for m in range(7 - n, 7)],
    )
    adapter = bc.BCAdapter()
    monkeypatch.setattr(adapter, "_company_url", lambda: "")
    monkeypatch.setattr(adapter, "_get", _get)

    rows = adapter.get_usage_entries(months=6)  # windows 2026-01 .. 2026-06
    assert len(filters) == 6                    # one request per trailing month
    assert all("Entry_Type eq 'Negative Adjmt.'" in f for f in filters)
    assert all("Entry_Type eq 'Consumption'" in f for f in filters)

    by_key = {(r["sku"], r["period"]): r["quantity"] for r in rows}
    assert by_key[("WTL175", "2026-06")] == 2444 + 2440 - 2000   # reversal nets off
    assert by_key[("BX186", "2026-05")] == 459
    assert by_key[("BX186", "2026-04")] == 0                     # clamps, never negative
