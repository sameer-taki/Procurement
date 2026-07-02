"""Demo data for the adapters.

Used when a source system is unconfigured (no DSN/creds) so the Stock view is
usable out of the box. This is NOT a substitute for the real interfaces — each
adapter falls back here only until the live read is wired (CLAUDE.md §7). Numbers
are fixed (not random) so tests and screenshots are deterministic.

Item master (sku/name/type/refs/price) is owned by BC.
Operational stock lives in Kiwiplan (corrugated + plant stores) and Accura (labels).

Paper roll stock (grade + deckle) follows the GML procurement SOP: imported in
40ft FCLs (25t) from overseas mills, tracked by grade AND deckle, planned to a
rolling 3-month cover. The demo figures deliberately put some grades below cover
so the Order Page has something to suggest.
"""
from datetime import date
from typing import Optional

from .planning import forward_periods, trailing_periods

# Each entry: the canonical item plus the live stock rows that the operational
# systems would report. `system` on a stock row is KIWIPLAN or ACCURA.
CATALOG = [
    {
        "sku": "BOARD-200K", "name": "Kraft Linerboard 200gsm", "item_type": "MATERIAL",
        "uom": "KG", "bc_item_no": "BC-1001", "is_purchased": True, "is_made": False,
        "reorder_point": 8000, "lead_time_days": 21, "sales_price": 1.95,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 12450, "allocated": 4200, "on_order": 6000},
            {"system": "KIWIPLAN", "location": "Lautoka Store", "on_hand": 3100, "allocated": 900, "on_order": 0},
        ],
    },
    {
        "sku": "BOARD-150F", "name": "Fluting Medium 150gsm", "item_type": "MATERIAL",
        "uom": "KG", "bc_item_no": "BC-1002", "is_purchased": True, "is_made": False,
        "reorder_point": 6000, "lead_time_days": 21, "sales_price": 1.62,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 4200, "allocated": 2600, "on_order": 9000},
        ],
    },
    {
        "sku": "TESTLINER-125", "name": "Test Liner 125gsm Roll", "item_type": "MATERIAL",
        "uom": "KG", "bc_item_no": "BC-1003", "is_purchased": True, "is_made": False,
        "reorder_point": 5000, "lead_time_days": 28, "sales_price": 1.40,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 9800, "allocated": 1200, "on_order": 0},
        ],
    },
    {
        "sku": "GLUE-STARCH", "name": "Starch Adhesive", "item_type": "MATERIAL",
        "uom": "KG", "bc_item_no": "BC-2001", "is_purchased": True, "is_made": False,
        "reorder_point": 1500, "lead_time_days": 10, "sales_price": 2.10,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Plant Store", "on_hand": 2400, "allocated": 300, "on_order": 0},
        ],
    },
    {
        "sku": "INK-FLEXO-CYAN", "name": "Flexo Ink Cyan", "item_type": "MATERIAL",
        "uom": "L", "bc_item_no": "BC-2002", "is_purchased": True, "is_made": False,
        "reorder_point": 200, "lead_time_days": 30, "sales_price": 14.50,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Plant Store", "on_hand": 95, "allocated": 40, "on_order": 0},
        ],
    },
    {
        "sku": "WIRE-STITCH", "name": "Stitching Wire 2.0mm", "item_type": "MATERIAL",
        "uom": "KG", "bc_item_no": "BC-2003", "is_purchased": True, "is_made": False,
        "reorder_point": 300, "lead_time_days": 14, "sales_price": 3.80,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Plant Store", "on_hand": 210, "allocated": 60, "on_order": 0},
        ],
    },
    {
        "sku": "LBL-SUB-PP", "name": "Self-adhesive PP Label Stock", "item_type": "MATERIAL",
        "uom": "M2", "bc_item_no": "BC-3001", "is_purchased": True, "is_made": False,
        "reorder_point": 2000, "lead_time_days": 35, "sales_price": 0.85,
        "stock": [
            {"system": "ACCURA", "location": "Label Materials", "on_hand": 5400, "allocated": 1800, "on_order": 0},
        ],
    },
    {
        "sku": "LBL-SUB-PAPER", "name": "Semi-gloss Paper Label Stock", "item_type": "MATERIAL",
        "uom": "M2", "bc_item_no": "BC-3002", "is_purchased": True, "is_made": False,
        "reorder_point": 2500, "lead_time_days": 28, "sales_price": 0.52,
        "stock": [
            {"system": "ACCURA", "location": "Label Materials", "on_hand": 1600, "allocated": 1200, "on_order": 5000},
        ],
    },
    {
        "sku": "LBL-RIBBON-TT", "name": "Thermal Transfer Ribbon 110mm", "item_type": "MATERIAL",
        "uom": "EA", "bc_item_no": "BC-3003", "is_purchased": True, "is_made": False,
        "reorder_point": 120, "lead_time_days": 21, "sales_price": 9.20,
        "stock": [
            {"system": "ACCURA", "location": "Label Materials", "on_hand": 340, "allocated": 80, "on_order": 0},
        ],
    },
    {
        "sku": "STRAP-PET-16", "name": "PET Strapping 16mm", "item_type": "MATERIAL",
        "uom": "M", "bc_item_no": "BC-2004", "is_purchased": True, "is_made": False,
        "reorder_point": 5000, "lead_time_days": 18, "sales_price": 0.12,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Plant Store", "on_hand": 18000, "allocated": 2000, "on_order": 0},
        ],
    },
    # ------------------------------------------------------------------- #
    # Paper roll stock by grade + deckle (SOP §3). SKU = <grade>-<deckle>,
    # mirroring how the BC item master keys one item per grade/deckle. All are
    # imported FCL paper: uom KG, long lead times, no reorder_point (the 3-month
    # cover rule replaces min/max for roll stock).
    # ------------------------------------------------------------------- #
    {
        "sku": "CWT140-1400", "name": "Coated White Top 140gsm 1400mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4001",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 45, "sales_price": 1.92,
        "grade": "CWT140", "deckle_mm": 1400,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 30000, "allocated": 6000, "on_order": 0},
        ],
    },
    {
        "sku": "CWT140-1950", "name": "Coated White Top 140gsm 1950mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4002",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 45, "sales_price": 1.92,
        "grade": "CWT140", "deckle_mm": 1950,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 30000, "allocated": 2000, "on_order": 0},
        ],
    },
    {
        "sku": "HP140-1490", "name": "Kraft Top Liner 140gsm 1490mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4003",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 45, "sales_price": 1.78,
        "grade": "HP140", "deckle_mm": 1490,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 40000, "allocated": 8000, "on_order": 0},
        ],
    },
    {
        "sku": "RF135-1000", "name": "Recycled Fluting 135gsm 1000mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4004",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 60, "sales_price": 1.45,
        "grade": "RF135", "deckle_mm": 1000,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 20000, "allocated": 3000, "on_order": 0},
        ],
    },
    {
        "sku": "BX186-1400", "name": "Kraft Linerboard 186gsm 1400mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4005",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 60, "sales_price": 1.62,
        "grade": "BX186", "deckle_mm": 1400,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 60000, "allocated": 5000, "on_order": 0},
        ],
    },
    {
        "sku": "BX200-1950", "name": "Kraft Linerboard 200gsm 1950mm",
        "item_type": "MATERIAL", "uom": "KG", "bc_item_no": "BC-4006",
        "is_purchased": True, "is_made": False, "reorder_point": None,
        "lead_time_days": 60, "sales_price": 1.68,
        "grade": "BX200", "deckle_mm": 1950,
        "stock": [
            {"system": "KIWIPLAN", "location": "Suva Roll Store", "on_hand": 4000, "allocated": 1500, "on_order": 0},
        ],
    },
    {
        "sku": "CTN-FIJIWATER-1L", "name": "Fiji Water 1L Shipper Carton",
        "item_type": "FINISHED", "uom": "EA", "bc_item_no": "BC-9003",
        "is_purchased": False, "is_made": True, "reorder_point": None,
        "lead_time_days": 7, "sales_price": 1.85,
        "stock": [
            {"system": "KIWIPLAN", "location": "Finished Goods", "on_hand": 5200, "allocated": 5200, "on_order": 0},
        ],
    },
    {
        "sku": "BOX-RSC-A", "name": "RSC Box 400x300x300", "item_type": "FINISHED",
        "uom": "EA", "bc_item_no": "BC-9001", "is_purchased": False, "is_made": True,
        "reorder_point": None, "lead_time_days": 5, "sales_price": 1.10,
        "stock": [
            {"system": "KIWIPLAN", "location": "Finished Goods", "on_hand": 8200, "allocated": 8200, "on_order": 0},
        ],
    },
    {
        "sku": "LABEL-1L-RANGE", "name": "Product Label 100x150 (1L)", "item_type": "FINISHED",
        "uom": "EA", "bc_item_no": "BC-9002", "is_purchased": False, "is_made": True,
        "reorder_point": None, "lead_time_days": 4, "sales_price": 0.06,
        "stock": [
            {"system": "ACCURA", "location": "Finished Goods", "on_hand": 24000, "allocated": 12000, "on_order": 0},
        ],
    },
]

_BY_SKU = {row["sku"]: row for row in CATALOG}


# --------------------------------------------------------------------------- #
# Vendors + vendor prices (BC owns these in reality; demo until BC is wired).
# Each vendor_price: price in FJD, moq, lead_time_days. Two vendors compete on a
# few SKUs so vendor selection (cheapest, tie-break lead time) is exercised.
# `bc_vendor_no` mirrors what the BC vendor master would expose.
# --------------------------------------------------------------------------- #
VENDORS = [
    {"name": "Pacific Paper & Board Ltd", "email": "sales@pacificpaper.example",
     "bc_vendor_no": "V-1001"},
    {"name": "Fiji Industrial Supplies", "email": "sales@fijiindustrial.example",
     "bc_vendor_no": "V-1002"},
    # Overseas paper mills (SOP §4): FCL import paper. MOQ = one 40ft FCL (25t).
    {"name": "Visy Board", "email": "orders@visy.example",
     "bc_vendor_no": "V-2001"},
    {"name": "Changle Numat (CSC)", "email": "sales@changlenumat.example",
     "bc_vendor_no": "V-2002"},
]

# {sku: [ {vendor_name, price, moq, lead_time_days}, ... ]}
VENDOR_PRICES = {
    "BOARD-200K": [
        {"vendor": "Pacific Paper & Board Ltd", "price": 1.80, "moq": 1000, "lead_time_days": 21},
        {"vendor": "Fiji Industrial Supplies", "price": 1.88, "moq": 500, "lead_time_days": 18},
    ],
    "BOARD-150F": [
        {"vendor": "Pacific Paper & Board Ltd", "price": 1.50, "moq": 1000, "lead_time_days": 21},
    ],
    "TESTLINER-125": [
        {"vendor": "Pacific Paper & Board Ltd", "price": 1.30, "moq": 1000, "lead_time_days": 28},
    ],
    "GLUE-STARCH": [
        {"vendor": "Fiji Industrial Supplies", "price": 1.95, "moq": 200, "lead_time_days": 10},
    ],
    "INK-FLEXO-CYAN": [
        # Same price from both vendors -> tie-break on the lower lead_time_days.
        {"vendor": "Pacific Paper & Board Ltd", "price": 13.50, "moq": 20, "lead_time_days": 30},
        {"vendor": "Fiji Industrial Supplies", "price": 13.50, "moq": 10, "lead_time_days": 20},
    ],
    "WIRE-STITCH": [
        {"vendor": "Fiji Industrial Supplies", "price": 3.50, "moq": 50, "lead_time_days": 14},
    ],
    "LBL-SUB-PP": [
        {"vendor": "Fiji Industrial Supplies", "price": 0.78, "moq": 500, "lead_time_days": 35},
    ],
    "LBL-SUB-PAPER": [
        {"vendor": "Fiji Industrial Supplies", "price": 0.48, "moq": 500, "lead_time_days": 28},
    ],
    "LBL-RIBBON-TT": [
        {"vendor": "Fiji Industrial Supplies", "price": 8.50, "moq": 24, "lead_time_days": 21},
    ],
    "STRAP-PET-16": [
        {"vendor": "Fiji Industrial Supplies", "price": 0.10, "moq": 2000, "lead_time_days": 18},
    ],
    # Import paper. Coated/white-top grades come from Visy, kraft/fluting from
    # Changle Numat; HP140 is quoted by both so the cheapest-vendor selection is
    # exercised on a paper grade.
    #
    # moq is deliberately None: the mills' 25-tonne / 40ft-FCL minimum is an
    # ORDER-level container constraint (grades/deckles are combined to fill each
    # container — SOP §8), enforced by the planning engine's per-vendor
    # consolidation. A per-LINE moq here would make Phase 3 PO creation bump each
    # grade to 25t (max(qty, moq)) and silently break that consolidation.
    "CWT140-1400": [
        {"vendor": "Visy Board", "price": 1.82, "moq": None, "lead_time_days": 45},
    ],
    "CWT140-1950": [
        {"vendor": "Visy Board", "price": 1.82, "moq": None, "lead_time_days": 45},
    ],
    "HP140-1490": [
        {"vendor": "Visy Board", "price": 1.70, "moq": None, "lead_time_days": 45},
        {"vendor": "Changle Numat (CSC)", "price": 1.64, "moq": None, "lead_time_days": 60},
    ],
    "RF135-1000": [
        {"vendor": "Changle Numat (CSC)", "price": 1.36, "moq": None, "lead_time_days": 60},
    ],
    "BX186-1400": [
        {"vendor": "Changle Numat (CSC)", "price": 1.52, "moq": None, "lead_time_days": 60},
    ],
    "BX200-1950": [
        {"vendor": "Changle Numat (CSC)", "price": 1.58, "moq": None, "lead_time_days": 60},
    ],
}


# --------------------------------------------------------------------------- #
# BOMs (Phase 4). Per CLAUDE.md §2 the app OWNS the top "kit" level of
# cross-system BOMs; only the material BOMs are MIRRORED read-only from
# production. BOX-RSC-A and LABEL-1L-RANGE are both top-level FINISHED kits, so
# their kit headers are owner=APP. (A mirrored material sub-bill would be modelled
# as its own nested BomHeader owned by KIWIPLAN/ACCURA under the APP kit; the demo
# has no such intermediate level yet — these kits explode straight to purchased
# material leaves.)
# Materials are purchased leaves (no BOM of their own). qty_per is per 1 unit of
# the parent (yield_qty 1.0); scrap_pct is a fraction (0.05 == 5%). These are the
# same SKUs the CATALOG/vendor_prices use so they resolve to seeded item_ids.
#
# Each entry: {sku: {owner, yield_qty, lines: [{component, qty_per, scrap_pct}]}}.
# --------------------------------------------------------------------------- #
BOMS = {
    "BOX-RSC-A": {
        "owner": "APP",
        "yield_qty": 1.0,
        "lines": [
            {"component": "BOARD-200K", "qty_per": 0.62, "scrap_pct": 0.05},
            {"component": "GLUE-STARCH", "qty_per": 0.02, "scrap_pct": 0.0},
            {"component": "WIRE-STITCH", "qty_per": 0.005, "scrap_pct": 0.0},
            {"component": "STRAP-PET-16", "qty_per": 0.5, "scrap_pct": 0.0},
        ],
    },
    "LABEL-1L-RANGE": {
        "owner": "APP",
        "yield_qty": 1.0,
        "lines": [
            {"component": "LBL-SUB-PP", "qty_per": 0.02, "scrap_pct": 0.0},
            {"component": "LBL-RIBBON-TT", "qty_per": 0.001, "scrap_pct": 0.0},
        ],
    },
    # The SOP's demand->paper bridge (steps 1-2): the board-grade spec of the
    # carton, as KG of each grade/deckle per carton (blank size x GSM, with the
    # trim/corr-out factors folded into scrap_pct). Exploding a carton forecast
    # through this bill yields the KG-per-grade requirement the Order Page nets.
    "CTN-FIJIWATER-1L": {
        "owner": "APP",
        "yield_qty": 1.0,
        "lines": [
            {"component": "CWT140-1400", "qty_per": 0.42, "scrap_pct": 0.04},
            {"component": "RF135-1000", "qty_per": 0.35, "scrap_pct": 0.05},
            {"component": "BX186-1400", "qty_per": 0.38, "scrap_pct": 0.04},
            {"component": "GLUE-STARCH", "qty_per": 0.03, "scrap_pct": 0.0},
        ],
    },
}


# --------------------------------------------------------------------------- #
# Paper usage history (SOP step 3): monthly consumption per grade/deckle as BC
# would export it (fed by Kiwiplan job usage). Six trailing months per SKU,
# oldest first; the labels are computed relative to today at call time so the
# demo Order Page always has a live-looking window. Quantities are KG.
# --------------------------------------------------------------------------- #
USAGE_KG_BY_SKU = {
    "CWT140-1400": [16500, 17800, 18600, 17200, 18900, 18000],
    "CWT140-1950": [7600, 8200, 8500, 7900, 8100, 7700],
    "HP140-1490": [21000, 22400, 23100, 21800, 22600, 22100],
    "RF135-1000": [11400, 12100, 12600, 11900, 12300, 11700],
    "BX186-1400": [14200, 15100, 15600, 14800, 15300, 14000],
    "BX200-1950": [4600, 5100, 5300, 4900, 5200, 4900],
}


def usage_entries(today: Optional[date] = None) -> list[dict]:
    """Monthly usage rows as the BC export would supply them:
    {sku, period, quantity} for the trailing six months per paper SKU."""
    periods = trailing_periods(6, today)
    out: list[dict] = []
    for sku, quantities in USAGE_KG_BY_SKU.items():
        for period, qty in zip(periods, quantities):
            out.append({"sku": sku, "period": period, "quantity": float(qty)})
    return out


def forecasts(today: Optional[date] = None) -> list[dict]:
    """Demo customer forecast (SOP step 1): cartons per finished item per month
    for the coming 3 months, as Sales/Customer Service would submit it."""
    p = forward_periods(3, today)
    return [
        {"customer": "Fiji Water", "item": "CTN-FIJIWATER-1L", "period": p[0], "qty_cartons": 42000},
        {"customer": "Fiji Water", "item": "CTN-FIJIWATER-1L", "period": p[1], "qty_cartons": 45000},
        {"customer": "Fiji Water", "item": "CTN-FIJIWATER-1L", "period": p[2], "qty_cartons": 40000},
    ]


def boms() -> list[dict]:
    """BOM definitions: one row per parent with its owner + component lines."""
    return [
        {"sku": sku, "owner": b["owner"], "yield_qty": b["yield_qty"],
         "lines": [dict(ln) for ln in b["lines"]]}
        for sku, b in BOMS.items()
    ]


def vendors() -> list[dict]:
    """Vendor master as BC would expose it (one row per vendor)."""
    return [dict(v) for v in VENDORS]


def vendor_prices() -> list[dict]:
    """Flat vendor-price rows: {sku, vendor, price, moq, lead_time_days}."""
    out: list[dict] = []
    for sku, rows in VENDOR_PRICES.items():
        for r in rows:
            out.append({"sku": sku, **r})
    return out


def list_items() -> list[dict]:
    """Item master as BC would expose it (one row per SKU)."""
    out = []
    for row in CATALOG:
        systems = {s["system"] for s in row["stock"]}
        out.append({
            "sku": row["sku"],
            "name": row["name"],
            "item_type": row["item_type"],
            "uom": row["uom"],
            "bc_item_no": row["bc_item_no"],
            "is_purchased": row["is_purchased"],
            "is_made": row["is_made"],
            "reorder_point": row["reorder_point"],
            "lead_time_days": row["lead_time_days"],
            # In a real master these are distinct system ids; demo uses the SKU.
            "kiwiplan_ref": row["sku"] if "KIWIPLAN" in systems else None,
            "accura_ref": row["sku"] if "ACCURA" in systems else None,
            "sales_price": row["sales_price"],
            # Paper attributes (grade + deckle); None for non-roll-stock items.
            "grade": row.get("grade"),
            "deckle_mm": row.get("deckle_mm"),
        })
    return out


def item_price(sku: str) -> Optional[float]:
    row = _BY_SKU.get(sku)
    return row["sales_price"] if row else None


# BC's paper-inventory figure per SKU (the item master's Inventory flowfield,
# maintained from Kiwiplan's usage postings). Mostly equal to the operational
# on-hand so the reconciliation view reads clean; RF135-1000 deliberately
# disagrees so the SOP §9 variance control has something to show in demo mode
# (a usage posting BC has that the roll store count doesn't reflect).
_BC_INVENTORY_VARIANCE_KG = {"RF135-1000": -760.0}


def bc_inventory() -> dict:
    """{sku: BC on-hand} as BCAdapter.get_inventory would report it."""
    return {
        row["sku"]: sum(s["on_hand"] for s in row["stock"])
        + _BC_INVENTORY_VARIANCE_KG.get(row["sku"], 0.0)
        for row in CATALOG
    }


def _stock_rows(ref: Optional[str], system: str) -> list[dict]:
    row = _BY_SKU.get(ref or "")
    if not row:
        return []
    return [
        {"location": s["location"], "on_hand": s["on_hand"],
         "allocated": s["allocated"], "on_order": s["on_order"]}
        for s in row["stock"] if s["system"] == system
    ]


def kiwiplan_stock(item_ref: Optional[str]) -> list[dict]:
    return _stock_rows(item_ref, "KIWIPLAN")


def accura_stock(item_ref: Optional[str]) -> list[dict]:
    return _stock_rows(item_ref, "ACCURA")
