"""Operational reports — the registers behind the Analytics KPIs, exportable.

Three registers from canonical data only (CLAUDE.md §2: BC owns money; these
mirror what the app already owns): the PO register, the GRN/receipt log, and
monthly spend by vendor. Every report serves JSON for the Reports screen and
CSV (?format=csv) for Excel — the procurement team's lingua franca — via one
shared serializer so the two views can never drift.

Read-only and open to any authenticated role, like the Analytics figures.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway.models import (
    ExternalRef,
    Item,
    POLine,
    PurchaseOrder,
    Receipt,
    Vendor,
)
# The crosswalk keys purchasing writes — imported so the reports can never
# drift from the writer's constants.
from .purchasing import (
    BC_GRN_TYPE,
    BC_PO_TYPE,
    PO_REF_ENTITY_KIND,
    RECEIPT_REF_ENTITY_KIND,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _csv_safe(value):
    """Neutralise spreadsheet formula injection. A cell whose text starts with
    = + - @ (or a leading tab/CR Excel strips before those) is executed as a
    formula when the CSV is opened — a vendor/SKU/item name sourced from BC could
    carry `=HYPERLINK(...)` or `=cmd|...`. Prefix such text cells with a single
    quote so Excel/Sheets treat them as literal text. Numbers pass through."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# The on-screen JSON preview is capped; the CSV export is always complete. Keeps
# a register that has grown to thousands of rows from shipping the whole table to
# the browser just to render a preview (download the CSV for the full set).
PREVIEW_LIMIT = 500


def _respond(rows: list[dict], columns: list[str], fmt: str, filename: str):
    """One serializer for both views: a capped JSON preview for the screen, the
    full CSV (same columns, same order) for download."""
    if fmt != "csv":
        preview = rows[:PREVIEW_LIMIT]
        return {"columns": columns, "rows": preview, "count": len(rows),
                "shown": len(preview), "truncated": len(rows) > len(preview),
                "as_of": datetime.utcnow().isoformat()}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({k: _csv_safe(v) for k, v in row.items()} for row in rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _bc_refs(session: Session, entity_kind: str, external_type: str) -> dict:
    return {
        r.entity_id: r.external_id
        for r in session.exec(
            select(ExternalRef).where(
                ExternalRef.entity_kind == entity_kind,
                ExternalRef.system == "BC",
                ExternalRef.external_type == external_type,
            )
        ).all()
    }


PO_COLUMNS = ["number", "vendor", "status", "created_at", "line_count",
              "ordered_qty", "received_qty", "received_pct", "total", "bc_po_no"]


@router.get("/purchase-orders")
def po_register(
    status_filter: str | None = Query(None, alias="status"),
    fmt: str = Query("json", alias="format"),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """The PO register: every purchase order with its fulfilment progress."""
    pos = session.exec(select(PurchaseOrder)).all()
    if status_filter:
        pos = [p for p in pos if p.status == status_filter]
    vendors = {v.id: v.name for v in session.exec(select(Vendor)).all()}
    lines_by_po: dict[str, list[POLine]] = {}
    for ln in session.exec(select(POLine)).all():
        lines_by_po.setdefault(ln.po_id, []).append(ln)
    received_by_po: dict[str, float] = {}
    for r in session.exec(select(Receipt)).all():
        received_by_po[r.po_id] = received_by_po.get(r.po_id, 0.0) + r.quantity
    bc_nos = _bc_refs(session, PO_REF_ENTITY_KIND, BC_PO_TYPE)

    rows = []
    for po in sorted(pos, key=lambda p: p.created_at, reverse=True):
        lines = lines_by_po.get(po.id, [])
        ordered = sum(ln.quantity for ln in lines)
        received = received_by_po.get(po.id, 0.0)
        rows.append({
            "number": po.number,
            "vendor": vendors.get(po.vendor_id),
            "status": po.status,
            "created_at": po.created_at.isoformat(),
            "line_count": len(lines),
            "ordered_qty": ordered,
            "received_qty": received,
            "received_pct": round(received / ordered, 4) if ordered else None,
            "total": po.total,
            "bc_po_no": bc_nos.get(po.id),
        })
    return _respond(rows, PO_COLUMNS, fmt, "po-register.csv")


RECEIPT_COLUMNS = ["grn_no", "po_number", "vendor", "sku", "quantity",
                   "received_at", "bc_grn_no"]


@router.get("/receipts")
def receipt_log(
    fmt: str = Query("json", alias="format"),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """The GRN log: every received line, traceable to its PO and BC receipt."""
    receipts = session.exec(select(Receipt)).all()
    pos = {p.id: p for p in session.exec(select(PurchaseOrder)).all()}
    vendors = {v.id: v.name for v in session.exec(select(Vendor)).all()}
    items = {it.id: it.sku for it in session.exec(select(Item)).all()}
    bc_grns = _bc_refs(session, RECEIPT_REF_ENTITY_KIND, BC_GRN_TYPE)

    rows = []
    for r in sorted(receipts, key=lambda r: r.received_at, reverse=True):
        po = pos.get(r.po_id)
        rows.append({
            "grn_no": r.grn_no,
            "po_number": po.number if po else None,
            "vendor": vendors.get(po.vendor_id) if po else None,
            "sku": items.get(r.item_id),
            "quantity": r.quantity,
            "received_at": r.received_at.isoformat(),
            "bc_grn_no": bc_grns.get(r.grn_no),
        })
    return _respond(rows, RECEIPT_COLUMNS, fmt, "receipt-log.csv")


SPEND_COLUMNS = ["period", "vendor", "spend"]


@router.get("/spend-by-month")
def spend_by_month(
    fmt: str = Query("json", alias="format"),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """Monthly spend per vendor: received quantity x ordered unit price, keyed
    to the month the goods arrived (the same received-not-ordered basis as the
    Analytics spend figure)."""
    receipts = session.exec(select(Receipt)).all()
    po_lines = {ln.id: ln for ln in session.exec(select(POLine)).all()}
    pos = {p.id: p for p in session.exec(select(PurchaseOrder)).all()}
    vendors = {v.id: v.name for v in session.exec(select(Vendor)).all()}

    by_key: dict[tuple, float] = {}
    for r in receipts:
        ln = po_lines.get(r.po_line_id) if r.po_line_id else None
        if ln is None:
            continue
        po = pos.get(r.po_id)
        vendor = vendors.get(po.vendor_id) if po else None
        period = r.received_at.strftime("%Y-%m")
        by_key[(period, vendor)] = by_key.get((period, vendor), 0.0) + r.quantity * ln.unit_price

    rows = [
        {"period": period, "vendor": vendor, "spend": round(amount, 2)}
        for (period, vendor), amount in sorted(by_key.items(), reverse=True)
    ]
    return _respond(rows, SPEND_COLUMNS, fmt, "spend-by-month.csv")
