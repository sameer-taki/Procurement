"""Paper planning — the SOP's Order Page (stock position & reorder determination).

Wires the pure coverage engine in `app.gateway.planning` to the canonical tables,
mirroring how bom_service wires `app.gateway.bom`:

  * Usage (SOP step 3): imported from BC's usage export into `usage_history`
    (the consumption originates in Kiwiplan and is passed to BC).
  * Forecast basis (step 4): the coming 3 months of customer forecasts (cartons)
    exploded through the BOMs to KG per paper grade/deckle.
  * History basis (step 5): items with no forecast fall back to the trailing
    average of prior months' movement.
  * Order determination (step 6 / §8): order = 3 x monthly usage + outstanding
    demand - on-hand - in-transit, consolidated per vendor into whole 25-tonne
    containers (1 x 40ft FCL).

In-transit is the app's own open PO volume (issued, not yet received) — the
canonical successor to the manual Visy workbook's "Outstanding Orders". The
stock snapshots' `on_order` column (what the source systems report) is shown on
the Stock screens but deliberately NOT added here, so a PO this app issued is
never counted twice.

The gateway stays the only writer of canonical state: a planning run only ever
emits an app-owned DRAFT requisition (source='coverage') which flows into the
Phase 2 approval lifecycle — exactly like the Phase 4 demand path.
"""
import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway import planning as engine
from ..gateway.bc import BCAdapter
from ..gateway.bom import explode
from ..gateway.models import (
    Forecast,
    Item,
    OrderEvent,
    POLine,
    PurchaseOrder,
    Receipt,
    Requisition,
    RequisitionLine,
    Shipment,
    StockSnapshot,
    UsageHistory,
    Vendor,
)
from . import requisitions as req_service
from .bom_service import _cheapest_vendor_price, make_bom_of

router = APIRouter(prefix="/api", tags=["planning"])

bc = BCAdapter()

# Who may import usage or turn the plan into a suggested requisition. Mirrors the
# stock/PO/bom mutator gate: OFFICER/ADMIN only.
PLANNER_ROLES = {"OFFICER", "ADMIN"}

REQ_ENTITY_KIND = "REQUISITION"

# PO states whose remaining (unreceived) quantity counts as in-transit.
OPEN_PO_STATES = {"PO_ISSUED", "ACKNOWLEDGED", "PARTIALLY_RECEIVED"}

# How many trailing months of movement back the non-forecast fallback (SOP step 5).
HISTORY_MONTHS = 3


def _require_planner(user: CurrentUser) -> None:
    if user.role_code not in PLANNER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(PLANNER_ROLES))}",
        )


# --------------------------------------------------------------------------- #
# Period helpers ("YYYY-MM" calendar months)
# --------------------------------------------------------------------------- #
def forward_periods(n: int, today: Optional[date] = None) -> list[str]:
    """The current month + the next n-1, oldest first."""
    today = today or date.today()
    year, month = today.year, today.month
    out: list[str] = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def trailing_periods(n: int, today: Optional[date] = None) -> list[str]:
    """The n calendar months before the current one, oldest first."""
    today = today or date.today()
    year, month = today.year, today.month
    out: list[str] = []
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f"{year:04d}-{month:02d}")
    return list(reversed(out))


# --------------------------------------------------------------------------- #
# Usage import (SOP step 3)
# --------------------------------------------------------------------------- #
def import_usage(session: Session) -> dict:
    """Import BC's usage export into usage_history. Upserts by (item, period) so a
    re-import refreshes rather than duplicates; rows for SKUs we don't carry are
    counted + skipped (the export can cover more of the ledger than the app).
    """
    rows = bc.get_usage_entries()
    items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
    now = datetime.utcnow()
    imported = 0
    skipped = 0
    for r in rows:
        item = items_by_sku.get(r.get("sku"))
        period = r.get("period")
        if item is None or not period:
            skipped += 1
            continue
        existing = session.exec(
            select(UsageHistory).where(
                UsageHistory.item_id == item.id, UsageHistory.period == period
            )
        ).first()
        if existing is None:
            existing = UsageHistory(item_id=item.id, period=period, quantity=0.0)
        existing.quantity = float(r.get("quantity") or 0)
        existing.source = r.get("source", "BC")
        existing.imported_at = now
        session.add(existing)
        imported += 1
    session.commit()
    return {"imported": imported, "skipped": skipped, "as_of": now.isoformat()}


# --------------------------------------------------------------------------- #
# The planning inputs, assembled per paper item
# --------------------------------------------------------------------------- #
def _paper_items(session: Session) -> list[Item]:
    """Roll stock is planned by grade + deckle; anything with a grade qualifies."""
    return session.exec(
        select(Item).where(Item.grade.is_not(None), Item.active == True)  # noqa: E712
    ).all()


def forecast_kg_by_item(session: Session, periods: list[str]) -> tuple[dict, list]:
    """Explode the window's carton forecasts through the BOMs -> KG per paper item.

    Returns ({paper_item_id: kg over the window}, [skipped forecast SKUs]) where a
    forecast is skipped when its finished item has no BOM to explode (surfaced so
    the planner can fix the master data — SOP §9 data integrity)."""
    forecasts = session.exec(
        select(Forecast).where(Forecast.period.in_(periods))
    ).all()
    cartons_by_parent: dict[str, float] = {}
    for f in forecasts:
        cartons_by_parent[f.item_id] = cartons_by_parent.get(f.item_id, 0.0) + f.qty_cartons

    bom_of = make_bom_of(session)
    kg_by_item: dict[str, float] = {}
    skipped: list[str] = []
    paper_ids = {it.id for it in _paper_items(session)}
    for parent_id, cartons in cartons_by_parent.items():
        if bom_of(parent_id) is None:
            parent = session.get(Item, parent_id)
            skipped.append(parent.sku if parent else parent_id)
            continue
        for mat, kg in explode(parent_id, cartons, bom_of).items():
            if mat in paper_ids:
                kg_by_item[mat] = kg_by_item.get(mat, 0.0) + kg
    return kg_by_item, sorted(skipped)


def _usage_history_by_item(session: Session, periods: list[str]) -> dict[str, list[float]]:
    """{item_id: [monthly quantities present in the trailing window]}"""
    rows = session.exec(
        select(UsageHistory).where(UsageHistory.period.in_(periods))
    ).all()
    out: dict[str, list[float]] = {}
    for r in sorted(rows, key=lambda r: r.period):
        out.setdefault(r.item_id, []).append(r.quantity)
    return out


def _open_po_qty_by_item(session: Session) -> dict[str, float]:
    """In-transit per item: ordered minus received across open POs (SOP step 8's
    'Outstanding Orders', read from the canonical PO/GRN tables)."""
    pos = session.exec(
        select(PurchaseOrder).where(PurchaseOrder.status.in_(OPEN_PO_STATES))
    ).all()
    if not pos:
        return {}
    po_ids = [po.id for po in pos]
    received: dict[str, float] = {}
    for r in session.exec(select(Receipt).where(Receipt.po_id.in_(po_ids))).all():
        if r.po_line_id:
            received[r.po_line_id] = received.get(r.po_line_id, 0.0) + r.quantity
    out: dict[str, float] = {}
    for ln in session.exec(select(POLine).where(POLine.po_id.in_(po_ids))).all():
        remaining = ln.quantity - received.get(ln.id, 0.0)
        if remaining > 0:
            out[ln.item_id] = out.get(ln.item_id, 0.0) + remaining
    return out


def _next_eta_by_item(session: Session) -> dict[str, str]:
    """Earliest open-shipment ETA per item (via the shipment's PO lines)."""
    shipments = session.exec(
        select(Shipment).where(
            Shipment.status.in_(("CONFIRMED", "ON_WATER")), Shipment.eta.is_not(None)
        )
    ).all()
    if not shipments:
        return {}
    po_ids = {s.po_id for s in shipments}
    lines = session.exec(select(POLine).where(POLine.po_id.in_(po_ids))).all()
    items_by_po: dict[str, set] = {}
    for ln in lines:
        items_by_po.setdefault(ln.po_id, set()).add(ln.item_id)
    out: dict[str, date] = {}
    for s in shipments:
        for item_id in items_by_po.get(s.po_id, ()):
            if item_id not in out or s.eta < out[item_id]:
                out[item_id] = s.eta
    return {item_id: eta.isoformat() for item_id, eta in out.items()}


def _stock_by_item(session: Session, item_ids: list[str]) -> dict[str, dict]:
    snaps = session.exec(
        select(StockSnapshot).where(StockSnapshot.item_id.in_(item_ids))
    ).all() if item_ids else []
    out: dict[str, dict] = {}
    for s in snaps:
        agg = out.setdefault(s.item_id, {"on_hand": 0.0, "allocated": 0.0, "as_of": None})
        agg["on_hand"] += s.on_hand
        agg["allocated"] += s.allocated
        if agg["as_of"] is None or s.as_of > agg["as_of"]:
            agg["as_of"] = s.as_of
    return out


# --------------------------------------------------------------------------- #
# The Order Page (SOP steps 3-6 in one view)
# --------------------------------------------------------------------------- #
def order_page(session: Session, today: Optional[date] = None) -> dict:
    """Assemble the coverage view per paper grade/deckle + the container plans."""
    items = _paper_items(session)
    item_ids = [it.id for it in items]

    window = forward_periods(engine.COVER_MONTHS, today)
    history_window = trailing_periods(HISTORY_MONTHS, today)
    forecast_kg, skipped_forecasts = forecast_kg_by_item(session, window)
    history = _usage_history_by_item(session, history_window)
    in_transit = _open_po_qty_by_item(session)
    next_eta = _next_eta_by_item(session)
    stock = _stock_by_item(session, item_ids)

    rows = []
    requirements_by_vendor: dict[Optional[str], dict[str, float]] = {}
    vendors_by_id: dict[str, Vendor] = {
        v.id: v for v in session.exec(select(Vendor)).all()
    }
    for it in items:
        st = stock.get(it.id, {"on_hand": 0.0, "allocated": 0.0, "as_of": None})
        if it.id in forecast_kg:
            basis = "FORECAST"
            monthly = forecast_kg[it.id] / engine.COVER_MONTHS
        elif history.get(it.id):
            basis = "HISTORY"
            monthly = engine.trailing_average(history[it.id])
        else:
            basis = "NONE"
            monthly = 0.0

        transit = in_transit.get(it.id, 0.0)
        months = engine.months_of_stock(st["on_hand"], transit, monthly)
        requirement = engine.order_quantity(
            monthly, st["allocated"], st["on_hand"], transit
        )

        vp = _cheapest_vendor_price(session, it.id)
        vendor = vendors_by_id.get(vp.vendor_id) if vp else None
        if requirement > 0:
            requirements_by_vendor.setdefault(
                vp.vendor_id if vp else None, {}
            )[it.id] = requirement

        rows.append({
            "sku": it.sku,
            "name": it.name,
            "grade": it.grade,
            "deckle_mm": it.deckle_mm,
            "uom": it.uom,
            "basis": basis,
            "monthly_usage": monthly,
            "usage_3mo": monthly * engine.COVER_MONTHS,
            "on_hand": st["on_hand"],
            "allocated": st["allocated"],
            "in_transit": transit,
            "next_eta": next_eta.get(it.id),
            "months_of_stock": months,
            "requirement_kg": requirement,
            "vendor": vendor.name if vendor else None,
            "lead_time_days": (vp.lead_time_days if vp else None) or it.lead_time_days,
            "as_of": st["as_of"].isoformat() if st["as_of"] else None,
        })

    plans = engine.plan_orders(requirements_by_vendor, block_kg=engine.KG_PER_FCL)
    items_by_id = {it.id: it for it in items}
    plan_out = []
    for p in plans:
        vendor = vendors_by_id.get(p.vendor_id) if p.vendor_id else None
        plan_out.append({
            "vendor_id": p.vendor_id,
            "vendor": vendor.name if vendor else None,
            "containers": p.containers,
            "total_kg": p.total_kg,
            "lines": [{
                "sku": items_by_id[ln.item_id].sku if ln.item_id in items_by_id else None,
                "requirement_kg": ln.requirement_kg,
                "order_kg": ln.order_kg,
            } for ln in p.lines],
        })

    rows.sort(key=lambda r: (r["grade"] or "", r["deckle_mm"] or 0))
    return {
        "cover_months": engine.COVER_MONTHS,
        "kg_per_fcl": engine.KG_PER_FCL,
        "window": window,
        "rows": rows,
        "container_plans": plan_out,
        "skipped_forecasts": skipped_forecasts,
        "below_cover": sum(
            1 for r in rows
            if r["months_of_stock"] is not None
            and r["months_of_stock"] < engine.COVER_MONTHS
        ),
    }


# --------------------------------------------------------------------------- #
# Suggested requisition (SOP step 6 -> the Phase 2 lifecycle)
# --------------------------------------------------------------------------- #
def suggest_orders(session: Session, user: CurrentUser,
                   cost_center: Optional[str] = None) -> dict:
    """Turn the current plan's container-consolidated quantities into ONE DRAFT
    requisition (source='coverage'), one line per grade/deckle. Reuses the Phase 2
    number scheme + audit so it is indistinguishable downstream. No requirement ->
    create nothing."""
    page = order_page(session)
    order_lines: list[tuple[str, float]] = []      # (sku, order_kg)
    for plan in page["container_plans"]:
        for ln in plan["lines"]:
            if ln["order_kg"] > 0 and ln["sku"]:
                order_lines.append((ln["sku"], ln["order_kg"]))
    if not order_lines:
        return {"created": False, "message": "all grades at or above cover"}

    items_by_sku = {
        it.sku: it for it in session.exec(
            select(Item).where(Item.sku.in_([sku for sku, _ in order_lines]))
        ).all()
    }
    order_lines.sort(key=lambda kv: kv[0])

    last_error: Optional[IntegrityError] = None
    for _ in range(5):
        req = Requisition(
            number=req_service._gen_number(),
            requester=user.email,
            status="DRAFT",
            source="coverage",
            cost_center=cost_center,
        )
        session.add(req)
        for sku, qty in order_lines:
            session.add(RequisitionLine(
                requisition_id=req.id, item_id=items_by_sku[sku].id, quantity=qty,
            ))
        session.add(OrderEvent(
            entity_kind=REQ_ENTITY_KIND,
            entity_id=req.id,
            from_status=None,
            to_status="DRAFT",
            event_type="CREATED",
            actor=user.email,
            detail_json=json.dumps({
                "source": "coverage",
                "cost_center": cost_center,
                "line_count": len(order_lines),
                "container_plans": page["container_plans"],
            }),
        ))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            last_error = exc
            continue
        session.refresh(req)
        detail = req_service._detail(session, req)
        detail["container_plans"] = page["container_plans"]
        return detail

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not allocate a unique requisition number",
    ) from last_error


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
class SuggestOrdersIn(BaseModel):
    cost_center: Optional[str] = None


@router.get("/planning/order-page")
def order_page_endpoint(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return order_page(session)


@router.post("/planning/import-usage")
def import_usage_endpoint(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_planner(user)
    return import_usage(session)


@router.post("/planning/suggest-orders")
def suggest_orders_endpoint(
    body: SuggestOrdersIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_planner(user)
    return suggest_orders(session, user, body.cost_center)
