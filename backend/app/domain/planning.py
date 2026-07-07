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
# Re-exported here because the planning window is service-level API surface
# (tests and callers reach them via this module).
from ..gateway.planning import forward_periods, trailing_periods  # noqa: F401
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
    VendorPrice,
)
from . import requisitions as req_service
from .bom_service import make_bom_of
from .purchasing import _choose_vendor_price

router = APIRouter(prefix="/api", tags=["planning"])

bc = BCAdapter()

# Who may import usage or turn the plan into a suggested requisition. Mirrors the
# stock/PO/bom mutator gate: OFFICER/ADMIN only.
PLANNER_ROLES = {"OFFICER", "ADMIN"}

REQ_ENTITY_KIND = "REQUISITION"

# PO states whose remaining (unreceived) quantity counts as in-transit. DRAFT is
# included deliberately: between create-po (requisition CLOSED) and issue, the
# volume exists only on the DRAFT PO — dropping it there would let the next
# planning run double-order it (SOP §9 in-transit accuracy). A dead DRAFT leaves
# the pipeline via CANCELLED.
OPEN_PO_STATES = {"DRAFT", "PO_ISSUED", "ACKNOWLEDGED", "PARTIALLY_RECEIVED"}

# Requisition states in which a coverage requisition is still in flight; a second
# planning run must not stack another full-tonnage order on top of one of these.
OPEN_REQ_STATES = {"DRAFT", "SUBMITTED", "IN_APPROVAL", "APPROVED"}

# How many trailing months of movement back the non-forecast fallback (SOP step 5).
HISTORY_MONTHS = 3

# BC-vs-production stock agreement tighter than this is treated as rounding, not
# a variance to investigate (both sides are KG figures for whole rolls).
RECON_TOLERANCE_KG = 1.0


def _require_planner(user: CurrentUser) -> None:
    if user.role_code not in PLANNER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(PLANNER_ROLES))}",
        )


# --------------------------------------------------------------------------- #
# Usage import (SOP step 3)
# --------------------------------------------------------------------------- #
def import_usage(session: Session) -> dict:
    """Import BC's usage export into usage_history. Upserts by (item, period) so a
    re-import refreshes rather than duplicates; rows for SKUs we don't carry are
    counted + skipped (the export can cover more of the ledger than the app).

    Check-then-write is raced by a concurrent import (two officers, or the ADMIN
    endpoint vs a scheduled run): the loser's INSERT hits
    uq_usage_history_item_period. One retry re-reads and lands as UPDATEs —
    same figures either way, since both imports carry the same BC export.
    """
    rows = bc.get_usage_entries()
    now = datetime.utcnow()
    last_error: Optional[IntegrityError] = None
    for _ in range(2):
        items_by_sku = {it.sku: it for it in session.exec(select(Item)).all()}
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
        try:
            session.commit()
        except IntegrityError as exc:   # lost the upsert race; retry as UPDATEs
            session.rollback()
            last_error = exc
            continue
        return {"imported": imported, "skipped": skipped, "as_of": now.isoformat()}
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A concurrent usage import is in progress; retry shortly",
    ) from last_error


# --------------------------------------------------------------------------- #
# The planning inputs, assembled per paper item
# --------------------------------------------------------------------------- #
def _paper_items(session: Session) -> list[Item]:
    """Roll stock is planned by grade + deckle; anything with a grade qualifies."""
    return session.exec(
        select(Item).where(Item.grade.is_not(None), Item.active == True)  # noqa: E712
    ).all()


def _ungraded_roll_skus(session: Session) -> list[str]:
    """Roll stock the planning run can't see: a deckle recorded but no grade.

    Since 'has a grade' is what admits an item to paper planning, a roll SKU whose
    BC master is missing the grade silently drops out of the Order Page — the
    SOP §9 data-integrity control says surface it, not swallow it."""
    return list(session.exec(
        select(Item.sku).where(
            Item.grade.is_(None),
            Item.deckle_mm.is_not(None),
            Item.active == True,  # noqa: E712
        ).order_by(Item.sku)
    ).all())


def forecast_kg_by_item(
    session: Session, periods: list[str]
) -> tuple[dict, dict, list]:
    """Explode the window's carton forecasts through the BOMs, per period.

    Returns ({paper_item_id: kg over the window}, {paper_item_id: covered-period
    count}, [skipped forecast SKUs]). Exploding period-by-period lets the caller
    average over the months a forecast actually COVERS instead of a fixed /3 —
    with forecasts entered one month at a time, a fixed divisor would read the
    not-yet-entered months as zero demand and silently under-plan. An explicit
    zero-carton forecast still covers its month (a real 'no demand' statement).

    A forecast is skipped when its finished item has no BOM to explode — or a
    broken (cyclic) one, which mirrored data could produce. Both are surfaced in
    skipped_forecasts so the planner can fix the master data (SOP §9 data
    integrity) while the rest of the Order Page still renders."""
    forecasts = session.exec(
        select(Forecast).where(Forecast.period.in_(periods))
    ).all()
    # {period: {parent_item_id: cartons}}
    by_period: dict[str, dict[str, float]] = {}
    for f in forecasts:
        parents = by_period.setdefault(f.period, {})
        parents[f.item_id] = parents.get(f.item_id, 0.0) + f.qty_cartons

    bom_of = make_bom_of(session)
    # Track per (paper item, PARENT): kg and the set of periods that parent
    # covers. Averaging per parent stops one fully-entered carton from masking
    # another carton's not-yet-entered months when both explode to the same
    # grade — pooling their periods would divide by the wider coverage and
    # under-state the partially-entered parent's monthly rate.
    kg_by_pair: dict[tuple, float] = {}
    periods_by_pair: dict[tuple, set] = {}
    skipped: set = set()
    paper_ids = {it.id for it in _paper_items(session)}
    for period, cartons_by_parent in by_period.items():
        for parent_id, cartons in cartons_by_parent.items():
            if bom_of(parent_id) is None:
                parent = session.get(Item, parent_id)
                skipped.add(parent.sku if parent else parent_id)
                continue
            try:
                exploded = explode(parent_id, cartons, bom_of)
            except ValueError:          # BOM cycle in mirrored data
                parent = session.get(Item, parent_id)
                skipped.add(parent.sku if parent else parent_id)
                continue
            for mat, kg in exploded.items():
                if mat in paper_ids:
                    key = (mat, parent_id)
                    kg_by_pair[key] = kg_by_pair.get(key, 0.0) + kg
                    periods_by_pair.setdefault(key, set()).add(period)
    # monthly[item] = sum over parents of (parent kg / that parent's months).
    # covered[item] = the widest parent coverage, for the FORECAST n/3 label.
    monthly: dict[str, float] = {}
    covered: dict[str, int] = {}
    for (mat, _parent), kg in kg_by_pair.items():
        n = len(periods_by_pair[(mat, _parent)]) or 1
        monthly[mat] = monthly.get(mat, 0.0) + kg / n
        covered[mat] = max(covered.get(mat, 0), n)
    # The engine takes (kg, periods) and divides; return an effective kg so
    # kg/covered reproduces the per-parent-averaged monthly rate exactly.
    effective_kg = {mat: monthly[mat] * covered[mat] for mat in monthly}
    return effective_kg, covered, sorted(skipped)


def _usage_history_by_item(session: Session, periods: list[str]) -> dict[str, list[float]]:
    """{item_id: [one quantity per in-scope window month, oldest first]} for every
    item with any usage history at all.

    A window month with no row counts as ZERO movement, NOT as absent — the BC
    usage export only emits rows for months with consumption postings, so a quiet
    month (plant down, grade not run) would otherwise vanish and inflate the
    average. BUT only for months at or after the item's FIRST-EVER usage row: a
    grade first bought two months ago must average over those two months, not be
    diluted by leading zeros for months before it existed (that under-planned a
    new grade by up to 3x). Items with no usage_history anywhere stay out of the
    result entirely (basis NONE, not a fake zero)."""
    tracked = set(session.exec(select(UsageHistory.item_id).distinct()).all())
    # Earliest recorded period per item, across ALL history (not just the window).
    first_period: dict[str, str] = {}
    for item_id, period in session.exec(
        select(UsageHistory.item_id, UsageHistory.period)
    ).all():
        if item_id not in first_period or period < first_period[item_id]:
            first_period[item_id] = period
    rows = session.exec(
        select(UsageHistory).where(UsageHistory.period.in_(periods))
    ).all()
    by_item_period: dict[str, dict[str, float]] = {}
    for r in rows:
        by_item_period.setdefault(r.item_id, {})[r.period] = r.quantity
    out: dict[str, list[float]] = {}
    for item_id in tracked:
        start = first_period.get(item_id, "")
        # Only window months from the item's first-ever usage month onward.
        scoped = [p for p in periods if p >= start]
        out[item_id] = [by_item_period.get(item_id, {}).get(p, 0.0) for p in scoped]
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

    # Resolve the clock ONCE so a run straddling midnight on the 1st can't get a
    # forward window from one day and a trailing window from the next.
    today = today or date.today()
    window = forward_periods(engine.COVER_MONTHS, today)
    history_window = trailing_periods(HISTORY_MONTHS, today)
    forecast_kg, forecast_periods, skipped_forecasts = forecast_kg_by_item(session, window)
    history = _usage_history_by_item(session, history_window)
    in_transit = _open_po_qty_by_item(session)
    next_eta = _next_eta_by_item(session)
    stock = _stock_by_item(session, item_ids)

    rows = []
    requirements_by_vendor: dict[Optional[str], dict[str, float]] = {}
    no_vendor: list[str] = []          # short grades with no vendor price to buy from
    vendors_by_id: dict[str, Vendor] = {
        v.id: v for v in session.exec(select(Vendor)).all()
    }
    # Chosen vendor price per paper item in ONE query, not a SELECT per item.
    # Group all prices for these items, then apply the SAME tie-break the Phase 3
    # helper uses (cheapest, then lower lead time) so selection is unchanged.
    prices_by_item: dict[str, list] = {}
    if item_ids:
        for vp in session.exec(
            select(VendorPrice).where(VendorPrice.item_id.in_(item_ids))
        ).all():
            prices_by_item.setdefault(vp.item_id, []).append(vp)
    chosen_vp = {
        item_id: _choose_vendor_price(vps)
        for item_id, vps in prices_by_item.items()
    }
    for it in items:
        st = stock.get(it.id, {"on_hand": 0.0, "allocated": 0.0, "as_of": None})
        history_avg = engine.trailing_average(history.get(it.id, []))
        covered = forecast_periods.get(it.id, 0)
        monthly, basis = engine.usage_basis(
            forecast_kg.get(it.id, 0.0), covered, history_avg
        )

        transit = in_transit.get(it.id, 0.0)
        months = engine.months_of_stock(st["on_hand"], transit, monthly)
        requirement = engine.order_quantity(
            monthly, st["allocated"], st["on_hand"], transit
        )

        vp = chosen_vp.get(it.id)
        vendor = vendors_by_id.get(vp.vendor_id) if vp else None
        # Only items with a real vendor go into the container plans. A shortage
        # with no vendor price can't become a PO (Phase 3 vendor selection drops
        # it), so consolidating it into a 25 t container and slack-topping it
        # would inflate a plan that then silently loses the line at PO creation.
        # Surface it as a 'needs a vendor price' warning instead.
        if requirement > 0 and vp is not None:
            requirements_by_vendor.setdefault(vp.vendor_id, {})[it.id] = requirement
        if requirement > 0 and vp is None:
            no_vendor.append(it.sku)

        rows.append({
            "sku": it.sku,
            "name": it.name,
            "grade": it.grade,
            "deckle_mm": it.deckle_mm,
            "uom": it.uom,
            "basis": basis,
            "monthly_usage": monthly,
            # Both candidate figures + coverage so the planner can see WHY a
            # basis won (e.g. a partial or below-movement forecast).
            "monthly_forecast": (
                forecast_kg.get(it.id, 0.0) / covered if covered else None
            ),
            "monthly_history": history_avg if it.id in history else None,
            "forecast_periods": covered,
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
    open_req = _open_coverage_req(session)
    return {
        "cover_months": engine.COVER_MONTHS,
        "kg_per_fcl": engine.KG_PER_FCL,
        "window": window,
        "rows": rows,
        "container_plans": plan_out,
        "skipped_forecasts": skipped_forecasts,
        # Roll stock excluded from planning because the item master has a deckle
        # but no grade — master-data fix needed in BC (SOP §9 data integrity).
        "ungraded_roll_skus": _ungraded_roll_skus(session),
        # Grades below cover that have no vendor price — can't be ordered until a
        # price is set in BC (they are deliberately kept out of container_plans).
        "no_vendor_skus": sorted(no_vendor),
        "below_cover": sum(
            1 for r in rows
            if r["months_of_stock"] is not None
            and r["months_of_stock"] < engine.COVER_MONTHS
        ),
        # An in-flight coverage requisition blocks a second suggest-orders run
        # (the duplicate-order guard); surfaced so the UI can link to it.
        "open_coverage_requisition": (
            {"id": open_req.id, "number": open_req.number, "status": open_req.status}
            if open_req else None
        ),
    }


# --------------------------------------------------------------------------- #
# Reconciliation (SOP §9: reconcile physical stock to BC; investigate variances
# by grade/deckle)
# --------------------------------------------------------------------------- #
def reconciliation(session: Session) -> dict:
    """BC's paper inventory vs the operational roll stock, per grade/deckle.

    BC maintains paper inventory from Kiwiplan's usage postings; the production
    systems report the physical roll stock the snapshots mirror. When the two
    drift (a usage posting missing in BC, an unposted receipt, a miscounted
    rack), the SOP's control is to investigate by grade/deckle — this view is
    that check. A paper item missing from BC's inventory read entirely is also
    flagged (it cannot be reconciled at all).
    """
    items = _paper_items(session)
    item_ids = [it.id for it in items]
    snaps = session.exec(
        select(StockSnapshot).where(StockSnapshot.item_id.in_(item_ids))
    ).all() if item_ids else []
    op: dict[str, dict] = {}
    for s in snaps:
        agg = op.setdefault(s.item_id, {"kg": 0.0, "systems": set(), "as_of": None})
        agg["kg"] += s.on_hand
        agg["systems"].add(s.system)
        if agg["as_of"] is None or s.as_of > agg["as_of"]:
            agg["as_of"] = s.as_of

    try:
        bc_inventory = bc.get_inventory()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"BC inventory read failed: {exc}",
        ) from exc

    rows = []
    for it in items:
        agg = op.get(it.id, {"kg": 0.0, "systems": set(), "as_of": None})
        bc_kg = bc_inventory.get(it.sku)
        variance = (bc_kg - agg["kg"]) if bc_kg is not None else None
        flagged = variance is None or abs(variance) > RECON_TOLERANCE_KG
        rows.append({
            "sku": it.sku,
            "grade": it.grade,
            "deckle_mm": it.deckle_mm,
            "operational_kg": agg["kg"],
            "systems": sorted(agg["systems"]),
            "bc_kg": bc_kg,
            "variance_kg": variance,
            "flagged": flagged,
            "as_of": agg["as_of"].isoformat() if agg["as_of"] else None,
        })
    # Flagged first, biggest discrepancy first; unreconcilable (no BC figure)
    # ahead of everything since there is nothing to net it against.
    rows.sort(key=lambda r: (
        not r["flagged"],
        -(abs(r["variance_kg"]) if r["variance_kg"] is not None else float("inf")),
        r["sku"],
    ))
    return {
        "tolerance_kg": RECON_TOLERANCE_KG,
        "checked": len(rows),
        "flagged": sum(1 for r in rows if r["flagged"]),
        "mode": "demo" if bc.use_fakes else "live",
        "rows": rows,
    }


def _open_coverage_req(session: Session) -> Optional[Requisition]:
    """The oldest still-in-flight coverage requisition, if any."""
    return session.exec(
        select(Requisition).where(
            Requisition.source == "coverage",
            Requisition.status.in_(OPEN_REQ_STATES),
        ).order_by(Requisition.created_at)
    ).first()


# --------------------------------------------------------------------------- #
# Suggested requisition (SOP step 6 -> the Phase 2 lifecycle)
# --------------------------------------------------------------------------- #
def suggest_orders(session: Session, user: CurrentUser,
                   cost_center: Optional[str] = None) -> dict:
    """Turn the current plan's container-consolidated quantities into ONE DRAFT
    requisition (source='coverage'), one line per grade/deckle. Reuses the Phase 2
    number scheme + audit so it is indistinguishable downstream. No requirement ->
    create nothing.

    Duplicate-order guard (SOP §9): while a previous coverage requisition is
    still in flight (DRAFT -> APPROVED, i.e. not yet turned into POs) the plan's
    shortages are already spoken for, so a second run is refused with 409 rather
    than stacking another full-tonnage order. Once POs exist the volume is
    counted as in-transit instead (OPEN_PO_STATES includes DRAFT)."""
    existing = _open_coverage_req(session)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Coverage requisition {existing.number} is still in flight "
                f"({existing.status}); action it before planning another order"
            ),
        )
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


@router.get("/planning/summary")
def planning_summary_endpoint(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """Slim coverage summary for the Dashboard tile — just the headline counts,
    not the full rows + container plans the Order Page ships. (The coverage math
    is the same, but the wire payload is a handful of ints instead of the whole
    register — the landing page shouldn't download the app's biggest response to
    render one number.)"""
    page = order_page(session)
    return {
        "cover_months": page["cover_months"],
        "below_cover": page["below_cover"],
        "grades_tracked": len(page["rows"]),
        "order_needed": len(page["container_plans"]) > 0,
        "no_vendor_skus": page["no_vendor_skus"],
        "open_coverage_requisition": page["open_coverage_requisition"],
    }


@router.get("/planning/reconciliation")
def reconciliation_endpoint(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return reconciliation(session)


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
