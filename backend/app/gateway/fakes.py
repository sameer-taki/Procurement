"""Demo data for the adapters.

Used when a source system is unconfigured (no DSN/creds) so the Stock view is
usable out of the box. This is NOT a substitute for the real interfaces — each
adapter falls back here only until the live read is wired (CLAUDE.md §7). Numbers
are fixed (not random) so tests and screenshots are deterministic.

Item master (sku/name/type/refs/price) is owned by BC.
Operational stock lives in Kiwiplan (corrugated + plant stores) and Accura (labels).
"""
from typing import Optional

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
}


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
        })
    return out


def item_price(sku: str) -> Optional[float]:
    row = _BY_SKU.get(sku)
    return row["sales_price"] if row else None


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
