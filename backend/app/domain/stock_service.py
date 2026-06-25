"""Stock service — the unified per-SKU stock view.

Pulls the item master from BC and live stock from Kiwiplan/Accura, then writes a
current snapshot per (item, system, location) into `stock_snapshots`. The gateway
is the only writer of canonical state; this never becomes a competing source of
stock truth — it caches what the systems report, stamped with `as_of`.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Session, delete, select

from ..config import settings
from ..gateway.accura import AccuraAdapter
from ..gateway.bc import BCAdapter
from ..gateway.kiwiplan import KiwiplanAdapter
from ..gateway.models import Item, ItemType, StockSnapshot

bc = BCAdapter()
kiwiplan = KiwiplanAdapter()
accura = AccuraAdapter()


def system_status() -> list[dict]:
    """Per-source freshness/mode, so the UI can flag demo vs live data."""
    return [
        {"system": "BC", "configured": settings.bc_enabled,
         "mode": "demo" if bc.use_fakes else "live"},
        {"system": "KIWIPLAN", "configured": settings.kiwiplan_enabled,
         "mode": "demo" if kiwiplan.use_fakes else "live"},
        {"system": "ACCURA", "configured": settings.accura_enabled,
         "mode": "demo" if accura.use_fakes else "live"},
    ]


def sync_items(session: Session) -> int:
    """Upsert the item master from BC (incl. cached price). Returns item count."""
    rows = bc.list_items()
    now = datetime.utcnow()
    for r in rows:
        item = session.exec(select(Item).where(Item.sku == r["sku"])).first()
        if item is None:
            item = Item(sku=r["sku"], name=r["name"], item_type=ItemType(r["item_type"]))
        item.name = r["name"]
        item.item_type = ItemType(r["item_type"])
        item.uom = r.get("uom", "EA")
        item.bc_item_no = r.get("bc_item_no")
        item.kiwiplan_ref = r.get("kiwiplan_ref")
        item.accura_ref = r.get("accura_ref")
        item.is_purchased = r.get("is_purchased", False)
        item.is_made = r.get("is_made", False)
        item.reorder_point = r.get("reorder_point")
        item.lead_time_days = r.get("lead_time_days")
        # Price comes with the master row when available; fall back to a per-item
        # lookup only if it doesn't.
        price = r.get("sales_price")
        if price is None:
            try:
                price = bc.get_item_price(r["sku"])
            except Exception:
                price = None
        if price is not None:
            item.sales_price = price
            item.price_synced_at = now
        session.add(item)
    session.commit()
    return len(rows)


def _rows_for(item: Item) -> list[tuple[str, list[dict]]]:
    """(system, rows) for each operational source that holds this item, skipping
    a source if its live read fails so one outage can't blank the whole view."""
    out: list[tuple[str, list[dict]]] = []
    for system, adapter, ref in (
        ("KIWIPLAN", kiwiplan, item.kiwiplan_ref),
        ("ACCURA", accura, item.accura_ref),
    ):
        if not ref:
            continue
        try:
            out.append((system, adapter.get_stock(ref)))
        except Exception:  # NotImplementedError or live connection error
            continue
    return out


def refresh_item(session: Session, item: Item) -> None:
    """Re-read this item's stock from its systems and replace its snapshots."""
    now = datetime.utcnow()
    session.exec(delete(StockSnapshot).where(StockSnapshot.item_id == item.id))
    for system, rows in _rows_for(item):
        for r in rows:
            on_hand = float(r.get("on_hand", 0) or 0)
            allocated = float(r.get("allocated", 0) or 0)
            on_order = float(r.get("on_order", 0) or 0)
            session.add(StockSnapshot(
                item_id=item.id, system=system, location=r.get("location"),
                on_hand=on_hand, allocated=allocated, on_order=on_order,
                available=on_hand - allocated + on_order, as_of=now,
            ))
    session.commit()


def refresh_all(session: Session) -> int:
    """Sync the master then refresh every item's stock. Returns items refreshed."""
    sync_items(session)
    items = session.exec(select(Item)).all()
    for item in items:
        refresh_item(session, item)
    return len(items)


def _snapshots(session: Session, item_id: str) -> list[StockSnapshot]:
    return session.exec(
        select(StockSnapshot).where(StockSnapshot.item_id == item_id)
    ).all()


def _totals(snaps: list[StockSnapshot]) -> dict:
    return {
        "on_hand": sum(s.on_hand for s in snaps),
        "allocated": sum(s.allocated for s in snaps),
        "on_order": sum(s.on_order for s in snaps),
        "available": sum(s.available for s in snaps),
    }


def _latest_as_of(snaps: list[StockSnapshot]) -> Optional[str]:
    if not snaps:
        return None
    return max(s.as_of for s in snaps).isoformat()


def _mode_for(system: str) -> str:
    adapter = {"KIWIPLAN": kiwiplan, "ACCURA": accura, "BC": bc}[system]
    return "demo" if adapter.use_fakes else "live"


def unified_view(session: Session, item: Item) -> dict:
    """Full per-SKU view: totals + per-system/location rows + price + freshness."""
    snaps = _snapshots(session, item.id)
    totals = _totals(snaps)
    by_system: dict[str, list[StockSnapshot]] = {}
    for s in snaps:
        by_system.setdefault(s.system, []).append(s)

    price = None
    if item.sales_price is not None:
        price = {
            "unit_price": item.sales_price,
            "currency": "FJD",
            "as_of": item.price_synced_at.isoformat() if item.price_synced_at else None,
        }

    below_reorder = (
        item.reorder_point is not None and totals["available"] < item.reorder_point
    )

    return {
        "sku": item.sku,
        "name": item.name,
        "item_type": item.item_type.value if hasattr(item.item_type, "value") else item.item_type,
        "uom": item.uom,
        "reorder_point": item.reorder_point,
        "lead_time_days": item.lead_time_days,
        "is_purchased": item.is_purchased,
        "is_made": item.is_made,
        "price": price,
        "totals": totals,
        "as_of": _latest_as_of(snaps),
        "below_reorder": below_reorder,
        "by_system": [
            {
                "system": sysname,
                "mode": _mode_for(sysname),
                "totals": _totals(rows),
                "rows": [
                    {
                        "location": r.location,
                        "on_hand": r.on_hand,
                        "allocated": r.allocated,
                        "on_order": r.on_order,
                        "available": r.available,
                        "as_of": r.as_of.isoformat(),
                    }
                    for r in rows
                ],
            }
            for sysname, rows in by_system.items()
        ],
    }


def search_items(session: Session, q: str = "", limit: int = 50) -> list[dict]:
    """Search the catalog by SKU or name; each result carries a stock summary."""
    stmt = select(Item).where(Item.active == True)  # noqa: E712
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Item.sku.ilike(like) | Item.name.ilike(like))
    items = session.exec(stmt.order_by(Item.sku).limit(limit)).all()
    out = []
    for item in items:
        snaps = _snapshots(session, item.id)
        totals = _totals(snaps)
        out.append({
            "sku": item.sku,
            "name": item.name,
            "item_type": item.item_type.value if hasattr(item.item_type, "value") else item.item_type,
            "uom": item.uom,
            "reorder_point": item.reorder_point,
            "totals": totals,
            "systems": sorted({s.system for s in snaps}),
            "as_of": _latest_as_of(snaps),
            "below_reorder": (
                item.reorder_point is not None and totals["available"] < item.reorder_point
            ),
        })
    return out


def dashboard(session: Session) -> dict:
    """Summary tiles for the Dashboard screen."""
    items = session.exec(select(Item)).all()
    snaps = session.exec(select(StockSnapshot)).all()
    by_item: dict[str, list[StockSnapshot]] = {}
    for s in snaps:
        by_item.setdefault(s.item_id, []).append(s)

    low = []
    for item in items:
        if item.reorder_point is None:
            continue
        avail = sum(s.available for s in by_item.get(item.id, []))
        if avail < item.reorder_point:
            low.append({"sku": item.sku, "name": item.name,
                        "available": avail, "reorder_point": item.reorder_point})

    return {
        "counts": {
            "items": len(items),
            "materials": sum(1 for i in items if i.item_type == ItemType.MATERIAL),
            "tracked_locations": len({(s.system, s.location) for s in snaps}),
            "below_reorder": len(low),
        },
        "low_stock": sorted(low, key=lambda x: x["available"]),
        "as_of": _latest_as_of(snaps),
        "systems": system_status(),
    }
