"""Phase 5 — Procurement analytics + warehouse export. All endpoints under /api.

Computes spend / on-time-delivery / stock-turn from CANONICAL data only (Receipt,
POLine, PurchaseOrder, Vendor, RequisitionLine, StockSnapshot). BC owns money and
the 3-way match (CLAUDE.md §2); these figures are operational analytics derived
from what the app already owns — not a competing source of financial truth.

The push endpoint exports the figures to the Azure SQL warehouse via the guarded
gateway.warehouse writer (the "figures land in the warehouse" DoD). In demo mode
the warehouse is unconfigured, so the writer no-ops and reports
'skipped:not-configured' without raising.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway import warehouse
from ..gateway.models import (
    POLine,
    PurchaseOrder,
    Receipt,
    RequisitionLine,
    StockSnapshot,
    Vendor,
)

router = APIRouter(prefix="/api", tags=["analytics"])

# Who may trigger a warehouse push. Read is any authed user.
PUSH_ROLES = {"ADMIN"}


# --------------------------------------------------------------------------- #
# Computations (canonical data only)
# --------------------------------------------------------------------------- #
def _spend(session: Session) -> dict:
    """Spend = sum(received_qty * po_line.unit_price). Total + by vendor.

    received_qty comes from Receipt rows (what actually arrived), priced at the
    ordered unit_price on the matching PO line — so spend reflects goods received,
    not merely ordered."""
    receipts = session.exec(select(Receipt)).all()
    po_lines = {ln.id: ln for ln in session.exec(select(POLine)).all()}
    pos = {p.id: p for p in session.exec(select(PurchaseOrder)).all()}
    vendor_names = {v.id: v.name for v in session.exec(select(Vendor)).all()}

    total = 0.0
    by_vendor: dict[str, float] = {}
    for r in receipts:
        po_line = po_lines.get(r.po_line_id) if r.po_line_id else None
        if po_line is None:
            continue
        amount = r.quantity * po_line.unit_price
        total += amount
        po = pos.get(r.po_id)
        vendor_id = po.vendor_id if po else None
        name = vendor_names.get(vendor_id, "Unknown")
        by_vendor[name] = by_vendor.get(name, 0.0) + amount

    return {
        "total": round(total, 2),
        "by_vendor": [
            {"vendor": name, "spend": round(amount, 2)}
            for name, amount in sorted(by_vendor.items(), key=lambda kv: -kv[1])
        ],
    }


def _on_time_delivery(session: Session) -> dict:
    """% of received lines (that have a needed_by) delivered on or before needed_by.

    needed_by lives on the source RequisitionLine. A PO traces to its requisition
    via PurchaseOrder.requisition_id; we match the PO line to the requisition line
    by item. Lines with no needed_by are excluded from the sample (can't judge)."""
    receipts = session.exec(select(Receipt)).all()
    po_lines = {ln.id: ln for ln in session.exec(select(POLine)).all()}
    pos = {p.id: p for p in session.exec(select(PurchaseOrder)).all()}

    # needed_by per (requisition_id, item_id) from the source requisition lines.
    needed_by: dict[tuple[str, str], object] = {}
    for rl in session.exec(select(RequisitionLine)).all():
        if rl.needed_by is not None:
            needed_by[(rl.requisition_id, rl.item_id)] = rl.needed_by

    sample = 0
    on_time = 0
    for r in receipts:
        po_line = po_lines.get(r.po_line_id) if r.po_line_id else None
        po = pos.get(r.po_id)
        if po_line is None or po is None or not po.requisition_id:
            continue
        due = needed_by.get((po.requisition_id, po_line.item_id))
        if due is None:
            continue
        sample += 1
        if r.received_at.date() <= due:
            on_time += 1

    rate = round(on_time / sample, 4) if sample else None
    return {"rate": rate, "sample": sample, "on_time": on_time}


def _stock_turn(session: Session) -> dict:
    """Indicative stock-turn proxy = sum(allocated) / sum(on_hand) across snapshots.

    This is a PROXY (true turn needs COGS over average inventory); the app displays
    stock but does not own cost truth, so it is labelled indicative. Divide-by-zero
    is guarded -> None when there is no on-hand."""
    snaps = session.exec(select(StockSnapshot)).all()
    on_hand = sum(s.on_hand for s in snaps)
    allocated = sum(s.allocated for s in snaps)
    value = round(allocated / on_hand, 4) if on_hand else None
    return {
        "value": value,
        "note": "indicative proxy: sum(allocated)/sum(on_hand), not COGS-based turn",
    }


def compute(session: Session) -> dict:
    """Assemble the analytics payload from canonical data, stamped with as_of."""
    return {
        "spend": _spend(session),
        "on_time_delivery": _on_time_delivery(session),
        "stock_turn": _stock_turn(session),
        "as_of": datetime.utcnow().isoformat(),
    }


def _warehouse_rows(figures: dict) -> dict[str, list[dict]]:
    """Flatten the figures into per-table row lists for the warehouse push."""
    as_of = figures["as_of"]
    spend = figures["spend"]
    otd = figures["on_time_delivery"]
    turn = figures["stock_turn"]
    return {
        "spend": [
            {"as_of": as_of, "vendor": row["vendor"], "spend": row["spend"]}
            for row in spend["by_vendor"]
        ] or ([{"as_of": as_of, "vendor": "ALL", "spend": spend["total"]}]
              if spend["total"] else []),
        "on_time_delivery": [
            {"as_of": as_of, "rate": otd["rate"],
             "sample": otd["sample"], "on_time": otd["on_time"]}
        ],
        "stock_turn": [
            {"as_of": as_of, "value": turn["value"], "note": turn["note"]}
        ],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/analytics")
def get_analytics(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """Spend / on-time-delivery / stock-turn from canonical data. Any authed user."""
    return compute(session)


@router.post("/analytics/push")
def push_analytics(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Compute the figures and push them to the Azure SQL warehouse (ADMIN only).

    This is the "figures land in the warehouse" DoD. The warehouse writer is guarded
    (gateway.warehouse.push): demo/unconfigured returns 'skipped:not-configured' per
    table and never raises; it writes for real once AZURE_SQL_DSN is set."""
    if user.role_code not in PUSH_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires role: ADMIN"
        )
    figures = compute(session)
    rows = _warehouse_rows(figures)
    pushed = {table: warehouse.push(table, table_rows)
              for table, table_rows in rows.items()}
    return {"figures": figures, "warehouse": pushed}
