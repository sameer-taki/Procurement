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
from ..gateway import fakes
from ..gateway.accura import AccuraAdapter
from ..gateway.bc import BCAdapter
from ..gateway.kiwiplan import KiwiplanAdapter
from ..gateway.models import (
    BomHeader,
    BomLine,
    BomOwner,
    Forecast,
    Item,
    ItemType,
    OrderEvent,
    StockSnapshot,
    UsageHistory,
    Vendor,
    VendorPrice,
)

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
    # One query for all existing items instead of a SELECT per SKU — at 5000+
    # items the per-row lookup dominated the 30-min refresh loop.
    existing = {
        it.sku: it for it in session.exec(select(Item)).all()
    }
    for r in rows:
        item = existing.get(r["sku"])
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
        # Paper attributes (SOP §3): stock is planned by grade AND deckle. BC
        # owns them, but only overwrite when the adapter actually supplies a
        # value — live _map_item returns None until the BC field mapping is
        # confirmed (INTEGRATIONS.md), and blanking grade on the first live sync
        # would silently empty the entire Order Page.
        if r.get("grade") is not None:
            item.grade = r.get("grade")
        if r.get("deckle_mm") is not None:
            item.deckle_mm = r.get("deckle_mm")
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


def seed_vendors(session: Session) -> int:
    """Seed demo vendors + vendor_prices when the vendor table is empty.

    BC owns the vendor master + prices in reality; until BC is wired we seed the
    demo set so PO vendor-selection works out of the box. Idempotent: no-op once
    any vendor exists. Returns the number of vendor_price rows after seeding.

    Runs only in BC demo mode (live BC will supply the real masters). Called from
    refresh_all so both startup and the test fixture get vendors for free.
    """
    if not bc.use_fakes:
        return len(session.exec(select(VendorPrice)).all())
    existing = session.exec(select(Vendor)).first()
    if existing is not None:
        return len(session.exec(select(VendorPrice)).all())

    by_name: dict[str, Vendor] = {}
    for v in fakes.vendors():
        vendor = Vendor(name=v["name"], email=v.get("email"),
                        bc_vendor_no=v.get("bc_vendor_no"))
        session.add(vendor)
        by_name[v["name"]] = vendor
    session.commit()

    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    count = 0
    for row in fakes.vendor_prices():
        item = items_by_sku.get(row["sku"])
        vendor = by_name.get(row["vendor"])
        if item is None or vendor is None:
            continue
        session.add(VendorPrice(
            vendor_id=vendor.id, item_id=item.id,
            price=row["price"], currency="FJD",
            moq=row.get("moq"), lead_time_days=row.get("lead_time_days"),
        ))
        count += 1
    session.commit()
    return count


def sync_vendors(session: Session) -> int:
    """Live-mode vendor master sync: upsert Vendor rows by bc_vendor_no from
    BC's vendor entity (BC owns vendors — CLAUDE.md §2; the app's table is a
    read-only mirror the PO workflow selects from). Demo mode no-ops: the demo
    seed (seed_vendors) owns that path. Never deletes — a vendor missing from a
    partial read must not orphan existing POs."""
    if bc.use_fakes:
        return 0
    rows = bc.list_vendors()
    by_no = {
        v.bc_vendor_no: v
        for v in session.exec(select(Vendor)).all() if v.bc_vendor_no
    }
    count = 0
    for r in rows:
        vendor = by_no.get(r["bc_vendor_no"])
        if vendor is None:
            vendor = Vendor(bc_vendor_no=r["bc_vendor_no"], name=r["name"])
            by_no[r["bc_vendor_no"]] = vendor
        vendor.name = r["name"]
        vendor.email = r.get("email")
        session.add(vendor)
        count += 1
    session.commit()
    return count


def sync_vendor_prices(session: Session) -> int:
    """Live-mode price-list sync: upsert VendorPrice by (vendor, item) from BC's
    purchase prices (BC owns price/SKU — CLAUDE.md §2). Rows for vendors/items
    the app doesn't carry are skipped; run after sync_vendors + sync_items."""
    if bc.use_fakes:
        return 0
    rows = bc.list_vendor_prices()
    vendors_by_no = {
        v.bc_vendor_no: v
        for v in session.exec(select(Vendor)).all() if v.bc_vendor_no
    }
    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    existing = {
        (vp.vendor_id, vp.item_id): vp
        for vp in session.exec(select(VendorPrice)).all()
    }
    count = 0
    for r in rows:
        vendor = vendors_by_no.get(r.get("vendor_no"))
        item = items_by_sku.get(r.get("sku"))
        if vendor is None or item is None:
            continue
        vp = existing.get((vendor.id, item.id))
        if vp is None:
            vp = VendorPrice(vendor_id=vendor.id, item_id=item.id, price=r["price"])
            existing[(vendor.id, item.id)] = vp
        vp.price = r["price"]
        vp.moq = r.get("moq")
        if r.get("lead_time_days") is not None:
            vp.lead_time_days = r["lead_time_days"]
        session.add(vp)
        count += 1
    session.commit()
    return count


def seed_boms(session: Session) -> int:
    """Seed demo BOM headers + lines when no BOM exists yet.

    The app owns the top kit level; the material bills are MIRRORED read-only from
    Kiwiplan/Accura (CLAUDE.md §2), so each header carries its owner. Idempotent:
    a no-op once any BomHeader exists, or when BC is live (the real masters/mirrors
    will supply BOMs then). Runs in the same demo-seed path vendors use so the test
    fixture and first boot both get BOMs. Returns the BomLine count after seeding.

    SKUs from fakes are resolved to seeded item_ids here; an unknown SKU (parent or
    component) skips that line so a partial catalog can't break seeding.
    """
    if not bc.use_fakes:
        return len(session.exec(select(BomLine)).all())
    if session.exec(select(BomHeader)).first() is not None:
        return len(session.exec(select(BomLine)).all())

    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    count = 0
    for bom in fakes.boms():
        parent = items_by_sku.get(bom["sku"])
        if parent is None:
            continue
        header = BomHeader(
            parent_item_id=parent.id,
            version=1,
            status="ACTIVE",
            owner=BomOwner(bom["owner"]),
            yield_qty=bom.get("yield_qty", 1.0),
            synced_at=datetime.utcnow(),
        )
        session.add(header)
        session.flush()  # need header.id for its lines
        for i, ln in enumerate(bom["lines"], start=1):
            component = items_by_sku.get(ln["component"])
            if component is None:
                continue
            session.add(BomLine(
                bom_header_id=header.id,
                line_no=i,
                component_id=component.id,
                qty_per=ln["qty_per"],
                uom=component.uom,
                scrap_pct=ln.get("scrap_pct", 0.0),
            ))
            count += 1
    session.commit()
    return count


# Marks rows/events written by the demo seeds, so the seeds can tell their own
# data from anything a user (or a real BC import) has touched and keep hands off.
DEMO_SEED_ACTOR = "demo-seed"


def seed_usage_history(session: Session) -> int:
    """Seed demo monthly usage (SOP step 3) for an out-of-the-box Order Page.

    In reality this is BC's usage export (Kiwiplan job consumption passed to BC),
    imported via POST /api/planning/import-usage; the seed only ever runs in demo
    mode. Because the trailing window is computed from today, seeded months age
    out: when EVERY row is still ours (source='demo-seed') and the newest one has
    fallen behind last month, the seed refreshes itself so a long-running demo
    doesn't quietly lose its usage basis. The moment any row has another source
    (a real/demo import rewrote it), we never touch the table again. Returns the
    usage_history row count."""
    if not bc.use_fakes:
        return len(session.exec(select(UsageHistory)).all())
    rows = session.exec(select(UsageHistory)).all()
    if rows:
        all_ours = all(r.source == DEMO_SEED_ACTOR for r in rows)
        newest = max(r.period for r in rows)
        last_month = fakes.trailing_periods(1)[-1]
        if not (all_ours and newest < last_month):
            return len(rows)
        for r in rows:                       # stale demo window: re-seed fresh
            session.delete(r)
        session.commit()

    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    count = 0
    for row in fakes.usage_entries():
        item = items_by_sku.get(row["sku"])
        if item is None:
            continue
        session.add(UsageHistory(
            item_id=item.id, period=row["period"], quantity=row["quantity"],
            source=DEMO_SEED_ACTOR,
        ))
        count += 1
    session.commit()
    return count


def _forecast_seed_marker(session: Session):
    return session.exec(
        select(OrderEvent).where(
            OrderEvent.entity_kind == "SYSTEM",
            OrderEvent.entity_id == "demo-forecasts",
            OrderEvent.event_type == "DEMO_FORECASTS_SEEDED",
        )
    ).first()


def seed_forecasts(session: Session) -> int:
    """Seed the demo customer forecast (SOP step 1), exactly once per database.

    Forecasts are app-owned user data (Sales/CS submit them), so the seed must
    never resurrect demand a planner deliberately deleted: a SYSTEM OrderEvent
    marker records that seeding has happened, and an empty table with the marker
    present stays empty. The one exception mirrors the usage seed: while every
    row is still untouched demo data (updated_by='demo-seed') and the whole
    window has aged into the past, the seed refreshes itself so a long-running
    demo keeps a live-looking forward window. Returns the forecast row count."""
    if not bc.use_fakes:
        return len(session.exec(select(Forecast)).all())
    rows = session.exec(select(Forecast)).all()
    current_month = fakes.forward_periods(1)[0]
    if rows:
        all_ours = all(f.updated_by == DEMO_SEED_ACTOR for f in rows)
        newest = max(f.period for f in rows)
        if not (all_ours and newest < current_month):
            return len(rows)
        for f in rows:                       # stale demo window: re-seed fresh
            session.delete(f)
        session.commit()
    elif _forecast_seed_marker(session) is not None:
        return 0                             # user deleted them; stay deleted

    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    count = 0
    for row in fakes.forecasts():
        item = items_by_sku.get(row["item"])
        if item is None:
            continue
        session.add(Forecast(
            customer=row["customer"], item_id=item.id, period=row["period"],
            qty_cartons=row["qty_cartons"], updated_by=DEMO_SEED_ACTOR,
        ))
        count += 1
    if _forecast_seed_marker(session) is None:
        session.add(OrderEvent(
            entity_kind="SYSTEM", entity_id="demo-forecasts",
            event_type="DEMO_FORECASTS_SEEDED", actor=DEMO_SEED_ACTOR,
        ))
    session.commit()
    return count


def _rows_for(item: Item) -> list[tuple[str, list[dict]]]:
    """(system, rows) for each operational source whose live read SUCCEEDED.

    A source whose read raises is omitted so the caller can leave its previous
    snapshot in place — see refresh_item. `out` only ever contains systems we
    actually re-read, never a system we failed to reach."""
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
    """Re-read this item's stock and replace ONLY the snapshots of the sources
    that answered.

    A source that is down must NOT blank the item: deleting every snapshot up
    front and rewriting only what came back would drop a temporarily-unreachable
    system's stock to zero, and the planning engine reads on_hand straight off
    these rows (a false zero over-orders whole containers). So we delete and
    rewrite per-successful-system and leave a failed source's prior rows intact —
    stale, with an old `as_of` the UI already surfaces as not-fresh, which is the
    honest state (CLAUDE.md: always show how fresh a figure is)."""
    now = datetime.utcnow()
    fresh = _rows_for(item)
    for system, rows in fresh:
        # Replace just THIS system's snapshots for the item.
        session.exec(
            delete(StockSnapshot).where(
                StockSnapshot.item_id == item.id,
                StockSnapshot.system == system,
            )
        )
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
    # Seed demo vendors + vendor_prices once the item master exists (no-op when a
    # vendor already exists, or when BC is live). Keeps PO vendor-selection usable
    # out of the box and makes vendors available to the test fixture.
    seed_vendors(session)
    # Live BC supplies the real vendor master + price list instead (each is a
    # no-op in demo mode, mirroring how seed_vendors no-ops in live mode).
    sync_vendors(session)
    sync_vendor_prices(session)
    # Seed demo BOMs the same way (no-op once any BOM exists / when BC is live) so
    # the explosion service + suggested requisitions work out of the box.
    seed_boms(session)
    # Seed demo paper usage + the demo customer forecast (no-op once any row
    # exists / when BC is live) so the paper-planning Order Page has a usage
    # basis and a forecast to explode out of the box.
    seed_usage_history(session)
    seed_forecasts(session)
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
        "grade": item.grade,
        "deckle_mm": item.deckle_mm,
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
            "grade": item.grade,
            "deckle_mm": item.deckle_mm,
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
