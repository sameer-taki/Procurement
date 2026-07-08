"""BC adapter LIVE-mode logic, with the OData transport mocked.

The live paths are built so 'going live' is a config exercise (INTEGRATIONS.md):
every entity set / field name / mapping convention is a setting. These tests
force live mode by pointing settings at a fake tenant and stubbing the
transport (_get/_send), then assert the adapter's wire behaviour: item-master
mapping (grade/deckle, crosswalk, lead time), retry-safe PO create + line
posting, receipt posting (Qty_to_Receive + post action) and the invoice-based
match signal. No network is touched.
"""
import pytest

from app.config import settings
from app.gateway.bc import BCAdapter, _parse_dateformula_days


@pytest.fixture()
def live(monkeypatch):
    """A BCAdapter in live mode against a fake tenant, with a scriptable
    transport. `calls` records every (method, url, body, if_match)."""
    monkeypatch.setattr(settings, "bc_base_url", "https://bc.test:7048/BC/ODataV4")
    monkeypatch.setattr(settings, "bc_company", "GML")
    monkeypatch.setattr(settings, "bc_username", "svc")
    monkeypatch.setattr(settings, "bc_password", "secret")
    adapter = BCAdapter()
    assert adapter.use_fakes is False

    calls: list[dict] = []
    responses: dict[str, list] = {"get": [], "send": []}

    def fake_get(url, params=None, session=None):
        calls.append({"method": "get", "url": url, "params": params or {}})
        return responses["get"].pop(0) if responses["get"] else {"value": []}

    def fake_send(method, url, body=None, if_match=None, session=None):
        calls.append({"method": method, "url": url, "body": body or {},
                      "if_match": if_match})
        return responses["send"].pop(0) if responses["send"] else {}

    monkeypatch.setattr(adapter, "_get", fake_get)
    monkeypatch.setattr(adapter, "_send", fake_send)
    adapter._test_calls = calls
    adapter._test_responses = responses
    return adapter


# --------------------------------------------------------------------------- #
# Item-master mapping: grade/deckle, crosswalk, reorder point, lead time
# --------------------------------------------------------------------------- #
def test_map_item_parses_grade_deckle_from_sku_convention():
    out = BCAdapter._map_item({"No": "CWT140-1400", "Description": "Coated White Top"})
    assert out["grade"] == "CWT140"
    assert out["deckle_mm"] == 1400


def test_map_item_non_paper_sku_gets_no_grade():
    out = BCAdapter._map_item({"No": "GLUE-STARCH", "Description": "Starch"})
    assert out["grade"] is None
    assert out["deckle_mm"] is None


def test_map_item_explicit_attribute_fields_win(monkeypatch):
    monkeypatch.setattr(settings, "bc_grade_field", "Paper_Grade")
    monkeypatch.setattr(settings, "bc_deckle_field", "Deckle_mm")
    out = BCAdapter._map_item({
        # The No matches the SKU convention with a DIFFERENT deckle — the
        # configured fields must win over the parse.
        "No": "CWT140-1400", "Paper_Grade": "CWT140", "Deckle_mm": "1950",
    })
    assert out["grade"] == "CWT140"
    assert out["deckle_mm"] == 1950


def test_map_item_crosswalk_sku_mode_is_default():
    out = BCAdapter._map_item({"No": "CWT140-1400"})
    assert out["kiwiplan_ref"] == "CWT140-1400"
    assert out["accura_ref"] == "CWT140-1400"


def test_map_item_crosswalk_none_mode(monkeypatch):
    monkeypatch.setattr(settings, "crosswalk_mode", "none")
    out = BCAdapter._map_item({"No": "CWT140-1400"})
    assert out["kiwiplan_ref"] is None
    assert out["accura_ref"] is None


def test_map_item_crosswalk_fields_mode(monkeypatch):
    monkeypatch.setattr(settings, "crosswalk_mode", "fields")
    monkeypatch.setattr(settings, "bc_kiwiplan_ref_field", "Kiwiplan_Code")
    monkeypatch.setattr(settings, "bc_accura_ref_field", "Accura_Code")
    out = BCAdapter._map_item({"No": "X-1", "Kiwiplan_Code": "KP77", "Accura_Code": ""})
    assert out["kiwiplan_ref"] == "KP77"
    assert out["accura_ref"] is None


def test_map_item_reorder_point_zero_means_unset():
    assert BCAdapter._map_item({"No": "X", "Reorder_Point": 0})["reorder_point"] is None
    assert BCAdapter._map_item({"No": "X", "Reorder_Point": 500})["reorder_point"] == 500


def test_map_item_replenishment_field_marks_finished(monkeypatch):
    monkeypatch.setattr(settings, "bc_replenishment_field", "Replenishment_System")
    made = BCAdapter._map_item({"No": "BOX-1", "Replenishment_System": "Prod. Order"})
    bought = BCAdapter._map_item({"No": "GLUE-1", "Replenishment_System": "Purchase"})
    assert made["item_type"] == "FINISHED" and made["is_made"] and not made["is_purchased"]
    assert bought["item_type"] == "MATERIAL" and bought["is_purchased"]


def test_parse_dateformula_days():
    assert _parse_dateformula_days("45D") == 45
    assert _parse_dateformula_days("<2W>") == 14
    assert _parse_dateformula_days("1M") == 30
    assert _parse_dateformula_days("") is None
    assert _parse_dateformula_days(None) is None
    assert _parse_dateformula_days("CW") is None


# --------------------------------------------------------------------------- #
# PO create: header + lines, retry-safe via External_Document_No
# --------------------------------------------------------------------------- #
PO_PAYLOAD = {
    "po_id": "po-1", "number": "PO-000123", "vendor_bc_no": "V-2001",
    "lines": [
        {"sku": "CWT140-1400", "bc_item_no": "CWT140-1400", "quantity": 50000, "unit_price": 1.92},
        {"sku": "RF135-1000", "bc_item_no": "RF135-1000", "quantity": 25000, "unit_price": 1.45},
    ],
}


def test_create_po_posts_header_then_lines(live):
    live._test_responses["get"] = [
        {"value": []},          # no existing header for PO-000123
        {"value": []},          # document has no lines yet
    ]
    live._test_responses["send"] = [{"No": "106001"}]   # header create returns the doc no

    assert live.create_purchase_order(PO_PAYLOAD) == "106001"

    sends = [c for c in live._test_calls if c["method"] == "post"]
    assert sends[0]["url"].endswith("/PurchaseOrders")
    assert sends[0]["body"]["External_Document_No"] == "PO-000123"
    assert sends[0]["body"]["Buy_from_Vendor_No"] == "V-2001"
    line_posts = sends[1:]
    assert [b["body"]["Line_No"] for b in line_posts] == [10000, 20000]
    assert line_posts[0]["body"] == {
        "Document_Type": "Order", "Document_No": "106001", "Line_No": 10000,
        "Type": "Item", "No": "CWT140-1400", "Quantity": 50000,
        "Direct_Unit_Cost": 1.92,
    }


def test_create_po_retry_reuses_existing_header_and_lines(live):
    # A full retry: the header exists AND both items are already on it (keyed by
    # item No, which is how BC returns lines and how reconciliation matches).
    live._test_responses["get"] = [
        {"value": [{"No": "106001"}]},                    # header already exists
        {"value": [
            {"Line_No": 10000, "No": "CWT140-1400"},
            {"Line_No": 20000, "No": "RF135-1000"},
        ]},                                               # both items already posted
    ]
    assert live.create_purchase_order(PO_PAYLOAD) == "106001"
    assert [c for c in live._test_calls if c["method"] == "post"] == []


def test_create_po_retry_completes_missing_lines(live):
    live._test_responses["get"] = [
        {"value": [{"No": "106001"}]},   # header exists (previous attempt died)
        {"value": []},                    # ...but its lines never landed
    ]
    assert live.create_purchase_order(PO_PAYLOAD) == "106001"
    posts = [c for c in live._test_calls if c["method"] == "post"]
    assert len(posts) == 2               # just the two lines, no second header


def test_create_po_retry_completes_only_the_missing_line(live):
    # Partial retry: one item landed before the crash, one didn't. Reconcile by
    # item No -> post ONLY the missing line, at a Line_No past the existing max
    # (this is the finding-#14 fix: don't drop lines a prior attempt missed).
    live._test_responses["get"] = [
        {"value": [{"No": "106001"}]},
        {"value": [{"Line_No": 10000, "No": "CWT140-1400"}]},   # only the first item
    ]
    assert live.create_purchase_order(PO_PAYLOAD) == "106001"
    posts = [c for c in live._test_calls if c["method"] == "post"]
    assert len(posts) == 1
    assert posts[0]["body"]["No"] == "RF135-1000"
    assert posts[0]["body"]["Line_No"] == 20000        # max existing (10000) + 10000


def test_create_po_uses_configurable_extref_field(live, monkeypatch):
    """BC14 doesn't expose External_Document_No on the PO page; the idempotency
    tag field is configurable. Header create AND the retry lookup must use it."""
    monkeypatch.setattr(settings, "bc_po_extref_field", "Vendor_Order_No")
    live._test_responses["get"] = [{"value": []}, {"value": []}]
    live._test_responses["send"] = [{"No": "106001"}]

    assert live.create_purchase_order(PO_PAYLOAD) == "106001"

    header = [c for c in live._test_calls if c["method"] == "post"][0]
    assert header["body"]["Vendor_Order_No"] == "PO-000123"
    assert "External_Document_No" not in header["body"]
    # the find-or-create lookup filters on the same field
    find = live._test_calls[0]
    assert find["params"]["$filter"] == "Vendor_Order_No eq 'PO-000123'"


def test_create_po_raises_when_bc_returns_no_number(live):
    live._test_responses["get"] = [{"value": []}]
    live._test_responses["send"] = [{}]            # header create came back empty
    with pytest.raises(RuntimeError, match="did not return a document No"):
        live.create_purchase_order(PO_PAYLOAD)


# --------------------------------------------------------------------------- #
# Receipt posting: Qty_to_Receive per line + post action + receipt-no readback
# --------------------------------------------------------------------------- #
RECEIPT_PAYLOAD = {
    "grn_no": "GRN-0001", "po_id": "po-1", "po_number": "PO-000123",
    "bc_po_no": "106001",
    "lines": [
        {"po_line_id": "l1", "item_id": "i1", "bc_item_no": "CWT140-1400", "quantity": 25000},
        {"po_line_id": "l2", "item_id": "i1", "bc_item_no": "CWT140-1400", "quantity": 5000},
    ],
}


def test_post_receipt_patches_qty_and_posts(live):
    live._test_responses["get"] = [
        {"value": [{"No": "CWT140-1400", "Line_No": 10000}]},   # order lines
        {"value": [{"No": "107001"}]},                            # posted receipt readback
    ]
    assert live.post_receipt(RECEIPT_PAYLOAD) == "107001"

    patches = [c for c in live._test_calls if c["method"] == "patch"]
    assert len(patches) == 1                                     # summed per item
    assert patches[0]["body"] == {"Qty_to_Receive": 30000}
    assert patches[0]["if_match"] == "*"
    assert "Line_No=10000" in patches[0]["url"]

    posts = [c for c in live._test_calls if c["method"] == "post"]
    assert posts[0]["url"].endswith("PurchaseOrders('106001')/Microsoft.NAV.Post")


def test_post_receipt_requires_posted_po(live):
    with pytest.raises(RuntimeError, match="not posted to BC yet"):
        live.post_receipt({**RECEIPT_PAYLOAD, "bc_po_no": None})


def test_post_receipt_unknown_item_on_order_fails_before_posting(live):
    live._test_responses["get"] = [{"value": []}]                # no matching order line
    with pytest.raises(RuntimeError, match="not on BC order"):
        live.post_receipt(RECEIPT_PAYLOAD)
    assert [c for c in live._test_calls if c["method"] in ("patch", "post")] == []


def test_post_receipt_readback_failure_falls_back_deterministically(live):
    """Once the post action succeeds the receipt EXISTS in BC — a readback
    hiccup must not raise (a retry would double-receive)."""
    live._test_responses["get"] = [
        {"value": [{"No": "CWT140-1400", "Line_No": 10000}]},
        {"value": []},                                            # readback finds nothing
    ]
    assert live.post_receipt(RECEIPT_PAYLOAD) == "BC-RCPT-GRN-0001"


def test_post_receipt_exactly_once_skips_when_already_posted(live, monkeypatch):
    """With BC_RECEIPT_CORRELATION_FIELD set, a retry after a lost Post response
    finds the receipt already carrying this grn_no and re-posts NOTHING."""
    monkeypatch.setattr(settings, "bc_receipt_correlation_field", "Vendor_Shipment_No")
    live._test_responses["get"] = [
        {"value": [{"No": "107001"}]},   # step 0: a posted receipt already has this grn
    ]
    assert live.post_receipt(RECEIPT_PAYLOAD) == "107001"
    # No writes at all — the whole point of exactly-once on retry.
    assert [c for c in live._test_calls if c["method"] in ("patch", "post")] == []
    assert "Vendor_Shipment_No eq 'GRN-0001'" in live._test_calls[0]["params"]["$filter"]


def test_post_receipt_stamps_correlation_then_reads_back_by_grn(live, monkeypatch):
    """First-time post with the correlation field set: stamp grn_no on the order
    header (so the receipt inherits it), receive, then read the receipt back by
    grn_no — exact even with several GRNs on one PO."""
    monkeypatch.setattr(settings, "bc_receipt_correlation_field", "Vendor_Shipment_No")
    live._test_responses["get"] = [
        {"value": []},                                            # step 0: not yet posted
        {"value": [{"No": "CWT140-1400", "Line_No": 10000}]},     # order lines
        {"value": [{"No": "107007"}]},                            # readback by grn_no
    ]
    assert live.post_receipt(RECEIPT_PAYLOAD) == "107007"

    patches = [c for c in live._test_calls if c["method"] == "patch"]
    # First write stamps the correlation field on the ORDER HEADER, before qty.
    assert patches[0]["body"] == {"Vendor_Shipment_No": "GRN-0001"}
    assert patches[0]["url"].endswith("PurchaseOrders('106001')")
    assert patches[0]["if_match"] == "*"
    assert patches[1]["body"] == {"Qty_to_Receive": 30000}

    posts = [c for c in live._test_calls if c["method"] == "post"]
    assert posts[0]["url"].endswith("PurchaseOrders('106001')/Microsoft.NAV.Post")


# --------------------------------------------------------------------------- #
# 3-way match: a posted purchase invoice IS the matched signal
# --------------------------------------------------------------------------- #
def test_match_status_matched_when_invoice_posted(live):
    live._test_responses["get"] = [{"value": [{"No": "108001"}]}]
    assert live.get_match_status({"bc_po_no": "106001"}) == "MATCHED"
    q = live._test_calls[0]
    assert q["url"].endswith("/PurchInvHeaders")
    assert q["params"]["$filter"] == "Order_No eq '106001'"


def test_match_status_pending_without_invoice(live):
    live._test_responses["get"] = [{"value": []}]
    assert live.get_match_status({"bc_po_no": "106001"}) == "PENDING_INVOICE"


def test_match_status_pending_when_po_never_posted(live):
    assert live.get_match_status({"bc_po_no": None, "po_number": None}) == "PENDING_INVOICE"
    assert live._test_calls == []          # no pointless BC round-trip


# --------------------------------------------------------------------------- #
# Vendor master + price list reads
# --------------------------------------------------------------------------- #
def test_list_vendors_maps_standard_fields(live):
    live._test_responses["get"] = [{"value": [
        {"No": "V-2001", "Name": "Visy Board", "E_Mail": "orders@visy.example"},
        {"No": "V-2002", "Name": "Changle Numat", "E_Mail": ""},
    ]}]
    out = live.list_vendors()
    assert out == [
        {"bc_vendor_no": "V-2001", "name": "Visy Board", "email": "orders@visy.example"},
        {"bc_vendor_no": "V-2002", "name": "Changle Numat", "email": None},
    ]


def test_list_vendor_prices_maps_purchase_prices(live):
    live._test_responses["get"] = [{"value": [
        {"Item_No": "CWT140-1400", "Vendor_No": "V-2001",
         "Direct_Unit_Cost": 1.85, "Minimum_Quantity": 25000},
        {"Item_No": "", "Vendor_No": "V-2001", "Direct_Unit_Cost": 9.99},  # dropped
    ]}]
    out = live.list_vendor_prices()
    assert out == [{
        "sku": "CWT140-1400", "vendor_no": "V-2001", "price": 1.85,
        "moq": 25000.0, "lead_time_days": None,
    }]


def test_demo_vendor_prices_carry_vendor_no():
    """Demo mode emits the same shape as live so sync code has one contract."""
    adapter = BCAdapter()
    assert adapter.use_fakes is True
    rows = adapter.list_vendor_prices()
    assert all("vendor_no" in r and "sku" in r for r in rows)
    visy = [r for r in rows if r["vendor_no"] == "V-2001"]
    assert visy, "demo Visy Board prices should map to V-2001"


def test_list_vendor_prices_skips_zero_cost(live):
    """A missing/<=0 Direct_Unit_Cost is 'not set' in BC — dropped so a synced
    0.0 can't win cheapest-vendor selection and post a free PO."""
    live._test_responses["get"] = [{"value": [
        {"Item_No": "A", "Vendor_No": "V1", "Direct_Unit_Cost": 0},
        {"Item_No": "B", "Vendor_No": "V1", "Direct_Unit_Cost": None},
        {"Item_No": "C", "Vendor_No": "V1", "Direct_Unit_Cost": 1.5},
    ]}]
    out = live.list_vendor_prices()
    assert [r["sku"] for r in out] == ["C"]


def test_get_inventory_walks_pages_and_coerces(live):
    """get_inventory paginates and coerces blank/missing Inventory to 0, skipping
    rows with no item No (a silent-zero mapping bug here would flood the
    reconciliation view with false variances)."""
    live._test_responses["get"] = [
        {"value": [{"No": "WTL175", "Inventory": 40000},
                   {"No": "BX186", "Inventory": None}],
         "@odata.nextLink": "page2"},
        {"value": [{"No": "", "Inventory": 999},          # no No -> skipped
                   {"No": "RF135", "Inventory": 12000}]},
    ]
    inv = live.get_inventory()
    assert inv == {"WTL175": 40000.0, "BX186": 0.0, "RF135": 12000.0}


def test_odata_str_escapes_single_quotes():
    from app.gateway.bc import _odata_str
    assert _odata_str("O'Brien Papers") == "O''Brien Papers"
    assert _odata_str("Golden Manufacturers Pte Ltd") == "Golden Manufacturers Pte Ltd"
    assert _odata_str(None) == ""


def test_company_url_escapes_apostrophe(monkeypatch):
    from app.config import settings
    from app.gateway.bc import BCAdapter
    monkeypatch.setattr(settings, "bc_base_url", "http://bc/ODataV4")
    monkeypatch.setattr(settings, "bc_company", "O'Brien Co")
    assert BCAdapter()._company_url() == "http://bc/ODataV4/Company('O''Brien Co')"


def test_list_items_selects_fields_and_skips_inventory_flowfield(live):
    """The items read must $select explicit columns so BC doesn't recompute the
    Inventory flowfield per row (the cause of the live timeout)."""
    live._test_responses["get"] = [
        {"value": [{"No": "WTL175", "Description": "White Top Liner", "Unit_Price": 1.9}]},
    ]
    items = live.list_items()
    assert items[0]["sku"] == "WTL175" and items[0]["sales_price"] == 1.9
    sel = live._test_calls[0]["params"]["$select"]
    assert "Inventory" not in sel
    for core in ("No", "Description", "Base_Unit_of_Measure", "Unit_Price"):
        assert core in sel


def test_list_items_falls_back_to_core_when_optional_field_absent(live, monkeypatch):
    """If a configured optional field isn't on the item page BC 400s; the read
    retries with core fields only so the whole sync doesn't fail."""
    import requests

    monkeypatch.setattr(settings, "bc_reorder_point_field", "Reorder_Point")
    calls: list[str] = []

    def flaky_get(url, params=None, session=None):
        sel = (params or {}).get("$select", "")
        calls.append(sel)
        if "Reorder_Point" in sel:                      # optional field not on the page
            resp = requests.Response()
            resp.status_code = 400
            raise requests.HTTPError("no property Reorder_Point", response=resp)
        return {"value": [{"No": "A", "Description": "Item A", "Unit_Price": 1.0}]}

    monkeypatch.setattr(live, "_get", flaky_get)
    items = live.list_items()
    assert [i["sku"] for i in items] == ["A"]
    assert len(calls) == 2                               # first (rich) 400s, retry (core) succeeds
    assert "Reorder_Point" in calls[0] and "Reorder_Point" not in calls[1]


def test_list_customers_walks_pages_and_maps(live):
    live._test_responses["get"] = [
        {"value": [{"No": "C-1001", "Name": "Fiji Water", "E_Mail": "o@fw"}],
         "@odata.nextLink": "p2"},
        {"value": [{"No": "", "Name": "skip"},          # no No -> skipped
                   {"No": "C-1002", "Name": "Pure Fiji", "E_Mail": None}]},
    ]
    out = live.list_customers()
    assert out == [
        {"bc_customer_no": "C-1001", "name": "Fiji Water", "email": "o@fw"},
        {"bc_customer_no": "C-1002", "name": "Pure Fiji", "email": None},
    ]
