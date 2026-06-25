"""Phase 3 — Purchase Orders: approved requisition -> PO -> post to BC -> email vendor.

The gateway is the only writer of canonical state (CLAUDE.md §2): this app owns
the PO workflow and decides every status transition; BC owns the *posted* PO and
returns its document number, which we store in `external_refs`. Posting goes
through the `integration_outbox` so it is reliable + retryable, and an idempotency
guard (the ExternalRef for this PO) makes a double-run NEVER double-post.

States: DRAFT -> PO_ISSUED -> ACKNOWLEDGED  (later: receiving in Phase 5).

Vendor selection: for each approved-requisition line, pick the cheapest
vendor_price (tie-break lower lead_time_days), then group chosen lines by vendor
into ONE PurchaseOrder per vendor. Order qty = max(requested_qty, moq or 0);
unit_price = the vendor's price; PO.total = sum(line qty * unit_price).
"""
import html
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from pydantic import BaseModel

from .. import mailer
from ..auth.deps import CurrentUser, get_current_user
from ..config import settings
from ..db import get_session
from ..gateway.bc import BCAdapter
from ..gateway.models import (
    ExternalRef,
    IntegrationOutbox,
    Item,
    OrderEvent,
    POLine,
    PurchaseOrder,
    Receipt,
    Requisition,
    RequisitionLine,
    Vendor,
    VendorPrice,
)
from . import stock_service

log = logging.getLogger("golden.procurement.purchasing")

router = APIRouter(prefix="/api", tags=["purchasing"])

bc = BCAdapter()

# OrderEvent.entity_kind for PO audit rows. The spec keeps this distinct from the
# ExternalRef crosswalk entity_kind below.
ENTITY_KIND = "PURCHASE_ORDER"
REQ_ENTITY_KIND = "REQUISITION"

# ExternalRef (crosswalk) entity_kind for the BC PO. The Phase 3 contract and the
# models.py docstring document the canonical value as 'PO' (distinct from the
# OrderEvent ENTITY_KIND='PURCHASE_ORDER'); using it here keeps cross-system
# lookups (e.g. Phase 5 receiving) aligned with the documented convention.
PO_REF_ENTITY_KIND = "PO"

# Outbox / BC integration constants.
OUTBOX_TARGET = "BC"
OUTBOX_ACTION = "create_purchase_order"
OUTBOX_ACTION_RECEIPT = "post_receipt"          # Phase 5 receipt posting
# The two BC-bound actions the outbox processor dispatches.
OUTBOX_ACTIONS = (OUTBOX_ACTION, OUTBOX_ACTION_RECEIPT)
BC_SYSTEM = "BC"
BC_PO_TYPE = "PURCHASE_ORDER"
MAX_ATTEMPTS = 5

# ExternalRef (crosswalk) entity_kind + types for Phase 5 receiving + match. A GRN
# is its own canonical entity (RECEIPT) keyed by the grn_no; the BC posted-receipt
# number is its 'GRN' crosswalk (the idempotency anchor for receipt posting). The
# match outcome BC reports is recorded as an INVOICE/MATCH crosswalk on the PO.
RECEIPT_REF_ENTITY_KIND = "RECEIPT"
BC_GRN_TYPE = "GRN"
BC_MATCH_TYPE = "INVOICE"

# PO states from which receiving is allowed.
RECEIVABLE_STATES = {"PO_ISSUED", "ACKNOWLEDGED", "PARTIALLY_RECEIVED"}

# Who may run the PO workflow (create/issue/receive). Mirrors stock's mutator gate.
PO_EDITOR_ROLES = {"OFFICER", "ADMIN"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gen_number() -> str:
    return f"PO-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:12]}"


def _require_po_editor(user: CurrentUser) -> None:
    if user.role_code not in PO_EDITOR_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(PO_EDITOR_ROLES))}",
        )


def _bad_transition(current: str, action: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot {action} a purchase order in state {current}",
    )


def _record_event(
    session: Session,
    *,
    entity_kind: str,
    entity_id: str,
    from_status: Optional[str],
    to_status: Optional[str],
    event_type: str,
    actor: Optional[str],
    detail: Optional[dict] = None,
) -> None:
    session.add(OrderEvent(
        entity_kind=entity_kind,
        entity_id=entity_id,
        from_status=from_status,
        to_status=to_status,
        event_type=event_type,
        actor=actor,
        detail_json=json.dumps(detail) if detail is not None else None,
    ))


def _po_events(session: Session, po_id: str) -> list[OrderEvent]:
    return session.exec(
        select(OrderEvent)
        .where(OrderEvent.entity_kind == ENTITY_KIND, OrderEvent.entity_id == po_id)
        .order_by(OrderEvent.id)
    ).all()


def _get_po(session: Session, po_id: str) -> PurchaseOrder:
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown purchase order")
    return po


def _po_lines(session: Session, po_id: str) -> list[POLine]:
    return session.exec(select(POLine).where(POLine.po_id == po_id)).all()


def _gen_grn() -> str:
    return f"GRN-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:8]}"


def _received_by_line(session: Session, po_id: str) -> dict[str, float]:
    """Cumulative received quantity per po_line_id for one PO (across all GRNs)."""
    out: dict[str, float] = {}
    for r in session.exec(select(Receipt).where(Receipt.po_id == po_id)).all():
        if r.po_line_id is None:
            continue
        out[r.po_line_id] = out.get(r.po_line_id, 0.0) + r.quantity
    return out


def _bc_ref(session: Session, po_id: str) -> Optional[ExternalRef]:
    """The crosswalk row proving this PO is already posted to BC, if any.
    This is the idempotency anchor: its presence means 'already posted'."""
    return session.exec(
        select(ExternalRef).where(
            ExternalRef.entity_kind == PO_REF_ENTITY_KIND,
            ExternalRef.entity_id == po_id,
            ExternalRef.system == BC_SYSTEM,
            ExternalRef.external_type == BC_PO_TYPE,
        )
    ).first()


def _grn_ref(session: Session, grn_no: str) -> Optional[ExternalRef]:
    """The crosswalk proving this GRN is already posted to BC, if any. This is the
    receipt idempotency anchor: its presence means 'already posted' (never re-post).
    Keyed by the canonical grn_no (entity_kind='RECEIPT')."""
    return session.exec(
        select(ExternalRef).where(
            ExternalRef.entity_kind == RECEIPT_REF_ENTITY_KIND,
            ExternalRef.entity_id == grn_no,
            ExternalRef.system == BC_SYSTEM,
            ExternalRef.external_type == BC_GRN_TYPE,
        )
    ).first()


def _match_ref(session: Session, po_id: str) -> Optional[ExternalRef]:
    """The crosswalk proving BC has reported a 3-way match for this PO, if any."""
    return session.exec(
        select(ExternalRef).where(
            ExternalRef.entity_kind == PO_REF_ENTITY_KIND,
            ExternalRef.entity_id == po_id,
            ExternalRef.system == BC_SYSTEM,
            ExternalRef.external_type == BC_MATCH_TYPE,
        )
    ).first()


# --------------------------------------------------------------------------- #
# Vendor selection
# --------------------------------------------------------------------------- #
def _choose_vendor_price(prices: list[VendorPrice]) -> VendorPrice:
    """Cheapest price; tie-break on the lower lead_time_days (None sorts last)."""
    return min(
        prices,
        key=lambda vp: (
            vp.price,
            vp.lead_time_days if vp.lead_time_days is not None else float("inf"),
        ),
    )


def _select_lines_by_vendor(
    session: Session, req_lines: list[RequisitionLine]
) -> tuple[dict[str, list[dict]], list[dict]]:
    """For each req line pick the cheapest vendor and bucket the chosen PO-line
    payload by vendor_id.

    Returns (chosen, skipped) where chosen is {vendor_id: [{item_id, sku, name,
    quantity, unit_price}, ...]} and skipped is [{item_id, sku, name, quantity}, ...]
    for lines that had NO vendor_price. Skipped lines are surfaced to the caller so
    they can be audited / returned rather than silently dropped."""
    chosen: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    for ln in req_lines:
        prices = session.exec(
            select(VendorPrice).where(VendorPrice.item_id == ln.item_id)
        ).all()
        item = session.get(Item, ln.item_id)
        if not prices:
            skipped.append({
                "item_id": ln.item_id,
                "sku": item.sku if item else None,
                "name": item.name if item else None,
                "quantity": ln.quantity,
            })
            continue
        vp = _choose_vendor_price(prices)
        moq = vp.moq or 0
        quantity = max(ln.quantity, moq)
        chosen.setdefault(vp.vendor_id, []).append({
            "item_id": ln.item_id,
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "quantity": quantity,
            "unit_price": vp.price,
        })
    return chosen, skipped


# --------------------------------------------------------------------------- #
# Serialisers
# --------------------------------------------------------------------------- #
def _summary(po: PurchaseOrder, vendor: Optional[Vendor],
             req_number: Optional[str], bc_po_no: Optional[str]) -> dict:
    return {
        "id": po.id,
        "number": po.number,
        "vendor": vendor.name if vendor else None,
        "status": po.status,
        "total": po.total,
        "requisition_id": po.requisition_id,
        "requisition_number": req_number,
        "bc_po_no": bc_po_no,
        "created_at": po.created_at.isoformat(),
    }


def _receipts_out(session: Session, po_id: str,
                  items: dict[str, Item]) -> list[dict]:
    """Serialise the goods receipts booked against a PO for the detail payload.

    The Receiving section (PurchaseOrderDetail.jsx) shows a "N GRNs booked" count
    and a booked-receipts table keyed on these rows. One GRN is several Receipt
    rows sharing a grn_no; we surface each Receipt line with its item SKU/name and
    the GRN's BC crosswalk (bc_grn_no) when the receipt has posted. The 3-way match
    is owned by BC and reflected at the PO level, so per-row match_status is left
    None here and the UI falls back to the PO-level match badge."""
    receipts = session.exec(
        select(Receipt).where(Receipt.po_id == po_id).order_by(Receipt.received_at)
    ).all()
    out: list[dict] = []
    grn_refs: dict[str, Optional[ExternalRef]] = {}
    for rc in receipts:
        item = items.get(rc.item_id) if rc.item_id else None
        if rc.grn_no not in grn_refs:
            grn_refs[rc.grn_no] = _grn_ref(session, rc.grn_no)
        ref = grn_refs[rc.grn_no]
        out.append({
            "grn_no": rc.grn_no,
            "bc_grn_no": ref.external_id if ref else None,
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "quantity": rc.quantity,
            "received_at": rc.received_at.isoformat(),
        })
    return out


def _detail(session: Session, po: PurchaseOrder) -> dict:
    lines = _po_lines(session, po.id)
    receipt_item_ids = {
        r.item_id
        for r in session.exec(select(Receipt).where(Receipt.po_id == po.id)).all()
        if r.item_id
    }
    item_ids = {ln.item_id for ln in lines} | receipt_item_ids
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_(item_ids))
        ).all()
    } if item_ids else {}
    received = _received_by_line(session, po.id)
    line_out = []
    for ln in lines:
        item = items.get(ln.item_id)
        line_out.append({
            "po_line_id": ln.id,
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "quantity": ln.quantity,
            "received_qty": received.get(ln.id, 0.0),
            "unit_price": ln.unit_price,
            "line_total": ln.quantity * ln.unit_price,
        })

    vendor = session.get(Vendor, po.vendor_id)
    ref = _bc_ref(session, po.id)
    match = _match_ref(session, po.id)
    req = session.get(Requisition, po.requisition_id) if po.requisition_id else None

    events = [{
        "from_status": e.from_status,
        "to_status": e.to_status,
        "event_type": e.event_type,
        "actor": e.actor,
        "detail": json.loads(e.detail_json) if e.detail_json else None,
        "occurred_at": e.occurred_at.isoformat(),
    } for e in _po_events(session, po.id)]
    email_status = next(
        (e["detail"].get("email_status")
         for e in reversed(events)
         if e["detail"] and "email_status" in e["detail"]),
        None,
    )

    return {
        "id": po.id,
        "number": po.number,
        "status": po.status,
        "total": po.total,
        "requisition_id": po.requisition_id,
        "requisition_number": req.number if req else None,
        "vendor": {"name": vendor.name if vendor else None,
                   "email": vendor.email if vendor else None},
        "bc_po_no": ref.external_id if ref else None,
        "matched": match is not None,
        "match_status": match.external_status if match else None,
        "bc_match_no": match.external_id if match else None,
        "email_status": email_status,
        "created_at": po.created_at.isoformat(),
        "lines": line_out,
        "receipts": _receipts_out(session, po.id, items),
        "events": events,
    }


# --------------------------------------------------------------------------- #
# PO creation (from an approved requisition)
# --------------------------------------------------------------------------- #
def _existing_pos_for_req(session: Session, req_id: str) -> list[PurchaseOrder]:
    return session.exec(
        select(PurchaseOrder).where(PurchaseOrder.requisition_id == req_id)
    ).all()


def create_pos_for_requisition(
    session: Session, req: Requisition, actor: str
) -> list[PurchaseOrder]:
    """Create vendor-grouped DRAFT POs for an APPROVED requisition and close it.

    Idempotent: if POs already exist for this requisition, return them unchanged
    (no duplicates). Caller must have validated the APPROVED state.

    If NO PO can be created (every line lacks a vendor_price), the requisition is
    left untouched (APPROVED) and an empty list is returned so the caller can fail
    the request and the req stays recoverable — it is NOT closed with zero POs.

    Lines whose item has no vendor_price are skipped from the POs but recorded in
    the requisition's PO_CREATED event detail (and returned via _last_skipped) so
    they are never silently dropped.
    """
    existing = _existing_pos_for_req(session, req.id)
    if existing:
        return existing

    req_lines = session.exec(
        select(RequisitionLine).where(RequisitionLine.requisition_id == req.id)
    ).all()
    by_vendor, skipped = _select_lines_by_vendor(session, req_lines)

    # No vendor price for ANY line -> nothing to order. Do NOT mutate the req: leave
    # it APPROVED and recoverable, and let the endpoint surface the failure.
    if not by_vendor:
        return []

    created: list[PurchaseOrder] = []
    for vendor_id, lines in by_vendor.items():
        total = sum(ln["quantity"] * ln["unit_price"] for ln in lines)
        # Retry on the (rare) PO-number collision with a fresh number. Use a
        # SAVEPOINT per insert so a collision rolls back ONLY this failed insert,
        # never the already-flushed POs for earlier vendors in this requisition.
        po: Optional[PurchaseOrder] = None
        last_error: Optional[IntegrityError] = None
        for _ in range(5):
            candidate = PurchaseOrder(
                number=_gen_number(),
                vendor_id=vendor_id,
                requisition_id=req.id,
                status="DRAFT",
                total=total,
            )
            sp = session.begin_nested()
            session.add(candidate)
            try:
                session.flush()
            except IntegrityError as exc:
                sp.rollback()
                last_error = exc
                continue
            po = candidate
            break
        if po is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not allocate a unique PO number",
            ) from last_error

        for ln in lines:
            session.add(POLine(
                po_id=po.id, item_id=ln["item_id"],
                quantity=ln["quantity"], unit_price=ln["unit_price"],
            ))
        _record_event(
            session,
            entity_kind=ENTITY_KIND, entity_id=po.id,
            from_status=None, to_status="DRAFT", event_type="PO_CREATED", actor=actor,
            detail={"requisition_id": req.id, "requisition_number": req.number,
                    "vendor_id": vendor_id, "line_count": len(lines), "total": total},
        )
        created.append(po)

    # Close the source requisition and audit on BOTH entities. Reached only when at
    # least one PO was created.
    prev = req.status
    req.status = "CLOSED"
    session.add(req)
    _record_event(
        session,
        entity_kind=REQ_ENTITY_KIND, entity_id=req.id,
        from_status=prev, to_status="CLOSED", event_type="PO_CREATED", actor=actor,
        detail={"po_ids": [p.id for p in created],
                "po_numbers": [p.number for p in created],
                "skipped_skus": [s["sku"] for s in skipped]},
    )
    session.commit()
    for po in created:
        session.refresh(po)
    return created


# --------------------------------------------------------------------------- #
# Outbox: enqueue + process (reliable, idempotent BC posting)
# --------------------------------------------------------------------------- #
def _pending_or_sent_outbox(session: Session, po_id: str) -> Optional[IntegrationOutbox]:
    """An existing create_purchase_order row for this PO that is not FAILED.
    Used to avoid enqueueing a duplicate on a re-issue. Indexed lookup on the
    first-class entity_ref column (no scan / no json parse)."""
    return session.exec(
        select(IntegrationOutbox).where(
            IntegrationOutbox.target == OUTBOX_TARGET,
            IntegrationOutbox.action == OUTBOX_ACTION,
            IntegrationOutbox.entity_ref == po_id,
            IntegrationOutbox.status != "FAILED",
        ).order_by(IntegrationOutbox.id)
    ).first()


def _po_payload(session: Session, po: PurchaseOrder) -> dict:
    """The BC create_purchase_order payload (also stored as the outbox request)."""
    vendor = session.get(Vendor, po.vendor_id)
    lines = _po_lines(session, po.id)
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_({ln.item_id for ln in lines}))
        ).all()
    } if lines else {}
    return {
        "po_id": po.id,
        "number": po.number,
        "vendor_id": po.vendor_id,
        "vendor_no": vendor.bc_vendor_no if vendor else None,
        "vendor_bc_no": vendor.bc_vendor_no if vendor else None,
        "total": po.total,
        "lines": [{
            "sku": items.get(ln.item_id).sku if items.get(ln.item_id) else None,
            "bc_item_no": items.get(ln.item_id).bc_item_no if items.get(ln.item_id) else None,
            "quantity": ln.quantity,
            "unit_price": ln.unit_price,
        } for ln in lines],
    }


def enqueue_po(session: Session, po: PurchaseOrder) -> IntegrationOutbox:
    """Enqueue a BC create_purchase_order outbox row for this PO, unless one is
    already pending/sent (so a re-issue never duplicates the work).

    The application-level check below is backed by a partial unique index (see the
    migration) so two concurrent issue calls cannot both insert a live row: the
    loser's INSERT raises IntegrityError, which we treat as 'already enqueued'."""
    existing = _pending_or_sent_outbox(session, po.id)
    if existing is not None:
        return existing
    row = IntegrationOutbox(
        target=OUTBOX_TARGET,
        action=OUTBOX_ACTION,
        entity_ref=po.id,
        request_json=json.dumps(_po_payload(session, po)),
        status="PENDING",
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent caller already enqueued a live row for this PO; reuse it.
        session.rollback()
        existing = _pending_or_sent_outbox(session, po.id)
        if existing is not None:
            return existing
        raise
    session.refresh(row)
    return row


def enqueue_receipt(session: Session, po: PurchaseOrder, grn_no: str,
                    received_lines: list[dict]) -> IntegrationOutbox:
    """Enqueue a BC post_receipt outbox row for one GRN.

    One outbox row per GRN (entity_ref=grn_no, action='post_receipt'). The payload
    carries grn_no + po_id (+ bc_po_no when known + the received lines) so the
    processor can post the receipt and reflect BC's match without re-querying. The
    partial unique index on (target, action, entity_ref) keeps it to one live row
    per GRN even under a racing enqueue; the loser reuses the existing row."""
    ref = _bc_ref(session, po.id)
    payload = {
        "grn_no": grn_no,
        "po_id": po.id,
        "po_number": po.number,
        "bc_po_no": ref.external_id if ref else None,
        "lines": received_lines,
    }
    row = IntegrationOutbox(
        target=OUTBOX_TARGET,
        action=OUTBOX_ACTION_RECEIPT,
        entity_ref=grn_no,
        request_json=json.dumps(payload),
        status="PENDING",
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent caller already enqueued a live row for this GRN; reuse it.
        session.rollback()
        existing = session.exec(
            select(IntegrationOutbox).where(
                IntegrationOutbox.target == OUTBOX_TARGET,
                IntegrationOutbox.action == OUTBOX_ACTION_RECEIPT,
                IntegrationOutbox.entity_ref == grn_no,
                IntegrationOutbox.status != "FAILED",
            ).order_by(IntegrationOutbox.id)
        ).first()
        if existing is not None:
            return existing
        raise
    session.refresh(row)
    return row


def _notify_vendor(session: Session, po: PurchaseOrder, bc_po_no: str) -> str:
    """Guarded vendor email after a successful BC post. Never raises."""
    vendor = session.get(Vendor, po.vendor_id)
    to = [vendor.email] if vendor and vendor.email else []
    subject = f"Purchase Order {po.number}"
    lines = _po_lines(session, po.id)
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_({ln.item_id for ln in lines}))
        ).all()
    } if lines else {}
    # HTML-escape every interpolated value: vendor name, SKU, PO/BC numbers all
    # originate from the BC/item master (untrusted), and the body is sent as HTML.
    # Numeric fields are formatted then escaped so a crafted value cannot inject
    # markup into the outbound email.
    def esc(value) -> str:
        return html.escape("" if value is None else str(value))

    rows_html = "".join(
        f"<tr><td>{esc(items.get(ln.item_id).sku if items.get(ln.item_id) else '')}</td>"
        f"<td>{esc(ln.quantity)}</td><td>{esc(f'{ln.unit_price:.2f}')}</td></tr>"
        for ln in lines
    )
    body = (
        f"<p>Dear {esc(vendor.name if vendor else 'Supplier')},</p>"
        f"<p>Please find our purchase order <b>{esc(po.number)}</b> "
        f"(BC ref {esc(bc_po_no)}).</p>"
        f"<table><tr><th>SKU</th><th>Qty</th><th>Unit price (FJD)</th></tr>"
        f"{rows_html}</table>"
        f"<p>Total: FJD {esc(f'{po.total:.2f}')}</p>"
        f"<p>Golden Manufactures Procurement</p>"
    )
    return mailer.notify(to, subject, body)


def _claim_row(session: Session, row_id: int) -> bool:
    """Atomically claim a PENDING outbox row for processing by flipping it to
    SENDING. Returns True iff THIS caller won the claim (rowcount == 1).

    This is the concurrency guard: two overlapping workers (issue-time inline run +
    background scheduler + ADMIN endpoint) cannot both proceed to POST the same row
    to BC — only the one whose conditional UPDATE matched a still-PENDING row does.
    """
    result = session.execute(
        update(IntegrationOutbox)
        .where(
            IntegrationOutbox.id == row_id,
            IntegrationOutbox.status == "PENDING",
        )
        .values(status="SENDING")
    )
    session.commit()
    return (result.rowcount or 0) == 1


def process_outbox(session: Session, *, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Process PENDING BC outbox rows. Reliable + idempotent; dispatch by action.

    Handles both BC-bound actions (CLAUDE.md Phase 3/5 DoD: NEVER double-post):
      * 'create_purchase_order' -> _process_po_row (Phase 3, unchanged)
      * 'post_receipt'          -> _process_receipt_row (Phase 5)

    For each PENDING row with attempts < max_attempts the loop CLAIMS it atomically
    (PENDING -> SENDING via a conditional UPDATE) so a concurrent worker cannot also
    process it, then dispatches to the per-action handler. Each handler enforces its
    own idempotency guard (an ExternalRef anchor): if the work is already done it
    marks the row SENT WITHOUT calling BC again. On failure it bumps attempts +
    last_error and returns the row to PENDING (or terminal FAILED at max_attempts).

    Running this twice yields exactly one BC post + one ExternalRef per entity.
    """
    counts = {"posted": 0, "skipped": 0, "failed": 0}
    rows = session.exec(
        select(IntegrationOutbox).where(
            IntegrationOutbox.target == OUTBOX_TARGET,
            IntegrationOutbox.action.in_(OUTBOX_ACTIONS),
            IntegrationOutbox.status == "PENDING",
        ).order_by(IntegrationOutbox.id)
    ).all()

    for row in rows:
        # Already exhausted but still PENDING (e.g. legacy data): retire it now.
        if row.attempts >= max_attempts:
            _mark_failed(session, row, "max attempts reached")
            counts["failed"] += 1
            continue

        # Claim atomically; a concurrent worker that grabbed it first wins.
        if not _claim_row(session, row.id):
            continue
        session.refresh(row)

        try:
            payload = json.loads(row.request_json)
        except (ValueError, TypeError) as exc:
            _record_attempt_failure(session, row, None, f"bad payload: {exc}",
                                    max_attempts)
            counts["failed"] += 1
            continue

        if row.action == OUTBOX_ACTION_RECEIPT:
            result = _process_receipt_row(session, row, payload, max_attempts)
        else:
            result = _process_po_row(session, row, payload, max_attempts)
        counts[result] += 1

    return counts


def _process_po_row(
    session: Session, row: IntegrationOutbox, payload: dict, max_attempts: int
) -> str:
    """Post one create_purchase_order row to BC. Returns 'posted'|'skipped'|'failed'.

    IDEMPOTENCY GUARD: if an ExternalRef already exists for this PO it is already
    posted -> mark SENT, never call BC again. On success write the ExternalRef
    (the anchor) first, mark SENT, set PO ACKNOWLEDGED, record an OrderEvent, then
    notify the vendor. A duplicate crosswalk insert (unique constraint) is treated
    as 'already posted' (defence in depth behind the claim)."""
    po_id = payload.get("po_id")
    po = session.get(PurchaseOrder, po_id) if po_id else None
    if po is None:
        _record_attempt_failure(session, row, po_id, f"unknown po_id {po_id}",
                                max_attempts)
        return "failed"

    # IDEMPOTENCY GUARD — already posted? Mark SENT, never call BC again.
    if _bc_ref(session, po.id) is not None:
        row.status = "SENT"
        session.add(row)
        session.commit()
        return "skipped"

    try:
        bc_po_no = bc.create_purchase_order(payload)
    except Exception as exc:  # back to PENDING for a retry (or FAILED if maxed)
        _record_attempt_failure(session, row, po.id, str(exc), max_attempts)
        return "failed"

    # Success: write the crosswalk FIRST (the idempotency anchor), then flip the
    # outbox + PO state in the same transaction. A unique-constraint collision here
    # means a racing worker already posted -> treat as 'already posted'.
    session.add(ExternalRef(
        entity_kind=PO_REF_ENTITY_KIND, entity_id=po.id,
        system=BC_SYSTEM, external_type=BC_PO_TYPE, external_id=bc_po_no,
        external_status="POSTED",
    ))
    row.status = "SENT"
    row.last_error = None
    session.add(row)
    prev = po.status
    po.status = "ACKNOWLEDGED"
    session.add(po)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status=prev, to_status="ACKNOWLEDGED", event_type="BC_POSTED",
        actor="system",
        detail={"bc_po_no": bc_po_no},
    )
    try:
        session.commit()
    except IntegrityError:
        # Lost a crosswalk race: the PO is already posted. Reconcile to SENT.
        session.rollback()
        session.refresh(row)
        row.status = "SENT"
        session.add(row)
        session.commit()
        return "skipped"

    # Notify the vendor (guarded; never raises) and audit the email status.
    email_status = _notify_vendor(session, po, bc_po_no)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status="ACKNOWLEDGED", to_status="ACKNOWLEDGED",
        event_type="VENDOR_NOTIFIED", actor="system",
        detail={"email_status": email_status},
    )
    session.commit()
    return "posted"


def _process_receipt_row(
    session: Session, row: IntegrationOutbox, payload: dict, max_attempts: int
) -> str:
    """Post one GRN (post_receipt) to BC, then reflect BC's 3-way match.
    Returns 'posted'|'skipped'|'failed'.

    IDEMPOTENCY GUARD: if a RECEIPT ExternalRef (external_type='GRN') already exists
    for this grn_no the receipt is already posted -> mark SENT, never call BC again.
    On success write that GRN crosswalk FIRST (the anchor), mark SENT, record a
    RECEIPT_POSTED event, then ask BC for the match status. BC owns the match
    (CLAUDE.md §2): when MATCHED we set the PO to MATCHED + write an INVOICE/MATCH
    crosswalk on the PO — we never fabricate money.

    Running this twice yields exactly one BC receipt post + one GRN ExternalRef."""
    grn_no = payload.get("grn_no")
    po_id = payload.get("po_id")
    po = session.get(PurchaseOrder, po_id) if po_id else None
    if not grn_no or po is None:
        _record_attempt_failure(session, row, po_id,
                                f"bad receipt payload grn={grn_no} po={po_id}",
                                max_attempts)
        return "failed"

    # IDEMPOTENCY GUARD — this GRN already posted to BC? Mark SENT, never re-post.
    if _grn_ref(session, grn_no) is not None:
        row.status = "SENT"
        session.add(row)
        session.commit()
        return "skipped"

    try:
        bc_grn_no = bc.post_receipt(payload)
    except Exception as exc:  # back to PENDING for a retry (or FAILED if maxed)
        _record_attempt_failure(session, row, po.id, str(exc), max_attempts)
        return "failed"

    # Success: write the GRN crosswalk FIRST (the idempotency anchor), mark the row
    # SENT, and audit — all in one transaction. A duplicate crosswalk insert (unique
    # constraint) means a racing worker already posted -> treat as 'already posted'.
    session.add(ExternalRef(
        entity_kind=RECEIPT_REF_ENTITY_KIND, entity_id=grn_no,
        system=BC_SYSTEM, external_type=BC_GRN_TYPE, external_id=bc_grn_no,
        external_status="POSTED",
    ))
    row.status = "SENT"
    row.last_error = None
    session.add(row)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status=po.status, to_status=po.status,
        event_type="RECEIPT_POSTED", actor="system",
        detail={"grn_no": grn_no, "bc_grn_no": bc_grn_no},
    )
    try:
        session.commit()
    except IntegrityError:
        # Lost a crosswalk race: the GRN is already posted. Reconcile to SENT.
        session.rollback()
        session.refresh(row)
        row.status = "SENT"
        session.add(row)
        session.commit()
        return "skipped"

    # BC owns the 3-way match (PO·GRN·invoice). Reflect (never fabricate) it.
    _reflect_match(session, po, payload, bc_grn_no)
    return "posted"


def _reflect_match(
    session: Session, po: PurchaseOrder, payload: dict, bc_grn_no: str
) -> None:
    """Ask BC for the PO's 3-way-match status and reflect it. When BC reports
    MATCHED we move the PO to MATCHED and write an INVOICE/MATCH crosswalk; we never
    invent invoice/money figures, only mirror BC's reported outcome. Guarded so a
    match-poll failure cannot undo the (already committed) receipt post.

    A 3-way match needs the WHOLE PO·GRN·invoice, so we only poll once the PO is
    fully received (status RECEIVED). A partial receipt leaves the PO
    PARTIALLY_RECEIVED and unmatched until the rest arrives."""
    session.refresh(po)
    if po.status != "RECEIVED":
        return
    try:
        match_status = bc.get_match_status(payload)
    except Exception as exc:  # pragma: no cover - match poll is best-effort
        log.warning("BC match status poll failed po=%s: %s", po.id, exc)
        return
    if match_status != "MATCHED":
        return
    if _match_ref(session, po.id) is not None:
        return
    session.add(ExternalRef(
        entity_kind=PO_REF_ENTITY_KIND, entity_id=po.id,
        system=BC_SYSTEM, external_type=BC_MATCH_TYPE, external_id=bc_grn_no,
        external_status="MATCHED",
    ))
    prev = po.status
    po.status = "MATCHED"
    session.add(po)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status=prev, to_status="MATCHED", event_type="MATCHED", actor="system",
        detail={"match_status": match_status, "bc_grn_no": bc_grn_no},
    )
    try:
        session.commit()
    except IntegrityError:
        # Lost a match-crosswalk race: already matched. Reconcile and move on.
        session.rollback()


def _mark_failed(session: Session, row: IntegrationOutbox, error: str) -> None:
    """Move an outbox row to the terminal FAILED state and audit the PO.

    The PO to audit is resolved from the payload, NOT from entity_ref: for
    create_purchase_order rows entity_ref IS the po_id, but for post_receipt rows
    entity_ref is the grn_no (enqueue_receipt). Parsing request_json first gives the
    real po_id for both actions, so the BC_POST_FAILED event lands on the correct PO
    instead of being silently skipped for a permanently failing GRN post."""
    row.status = "FAILED"
    if error:
        row.last_error = error
    session.add(row)
    try:
        payload = json.loads(row.request_json)
    except (ValueError, TypeError):
        payload = {}
    po_id = (payload.get("po_id") if isinstance(payload, dict) else None) \
        or row.entity_ref
    if po_id and session.get(PurchaseOrder, po_id) is not None:
        _record_event(
            session,
            entity_kind=ENTITY_KIND, entity_id=po_id,
            from_status=None, to_status=None, event_type="BC_POST_FAILED",
            actor="system",
            detail={"error": row.last_error, "attempts": row.attempts},
        )
    session.commit()


def _record_attempt_failure(
    session: Session, row: IntegrationOutbox, po_id: Optional[str],
    error: str, max_attempts: int,
) -> None:
    """Record one failed attempt: bump attempts + last_error, then either return the
    row to PENDING for a later retry, or — if attempts has reached max_attempts —
    retire it to the terminal FAILED state (operator-visible, not a silent zombie)."""
    row.attempts += 1
    row.last_error = error
    log.warning("BC PO post attempt failed po=%s attempt=%s: %s",
                po_id, row.attempts, error)
    if row.attempts >= max_attempts:
        _mark_failed(session, row, error)
    else:
        row.status = "PENDING"
        session.add(row)
        session.commit()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/requisitions/{req_id}/create-po", status_code=status.HTTP_201_CREATED)
def create_po(
    req_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_po_editor(user)
    req = session.get(Requisition, req_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown requisition")

    # Idempotent: if POs already exist for this req, return them (don't duplicate).
    existing = _existing_pos_for_req(session, req.id)
    if existing:
        return [_detail(session, po) for po in existing]

    if req.status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot create a PO from a requisition in state {req.status}",
        )

    pos = create_pos_for_requisition(session, req, user.email)
    if not pos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No vendor prices for any line; cannot create a purchase order",
        )
    return [_detail(session, po) for po in pos]


@router.get("/purchase-orders")
def list_purchase_orders(
    status_filter: Optional[str] = Query(None, alias="status"),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    stmt = select(PurchaseOrder)
    if status_filter:
        stmt = stmt.where(PurchaseOrder.status == status_filter)
    pos = session.exec(stmt.order_by(PurchaseOrder.created_at.desc())).all()

    vendors = {v.id: v for v in session.exec(select(Vendor)).all()}
    req_numbers = {
        r.id: r.number for r in session.exec(select(Requisition)).all()
    }
    refs = {
        r.entity_id: r.external_id
        for r in session.exec(
            select(ExternalRef).where(
                ExternalRef.entity_kind == PO_REF_ENTITY_KIND,
                ExternalRef.system == BC_SYSTEM,
                ExternalRef.external_type == BC_PO_TYPE,
            )
        ).all()
    }
    return [
        _summary(po, vendors.get(po.vendor_id),
                 req_numbers.get(po.requisition_id), refs.get(po.id))
        for po in pos
    ]


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(
    po_id: str,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return _detail(session, _get_po(session, po_id))


@router.post("/purchase-orders/{po_id}/issue")
def issue_purchase_order(
    po_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_po_editor(user)
    po = _get_po(session, po_id)
    if po.status != "DRAFT":
        raise _bad_transition(po.status, "issue")

    prev = po.status
    po.status = "PO_ISSUED"
    session.add(po)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status=prev, to_status="PO_ISSUED", event_type="PO_ISSUED", actor=user.email,
    )
    session.commit()

    # Enqueue (idempotent). Optionally drain inline for an immediate post; posting
    # is race-safe vs the background scheduler (per-row claim + unique crosswalk),
    # but operators can disable the inline run to avoid overlap entirely.
    enqueue_po(session, po)
    if settings.outbox_process_on_issue:
        process_outbox(session)
    session.refresh(po)
    return _detail(session, po)


@router.post("/outbox/process")
def process_outbox_endpoint(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    if user.role_code != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires role: ADMIN"
        )
    return process_outbox(session)


# --------------------------------------------------------------------------- #
# Phase 5 — Receiving (GRN capture against a PO)
# --------------------------------------------------------------------------- #
class ReceiveLineIn(BaseModel):
    po_line_id: str
    quantity: float
    location: Optional[str] = None


class ReceiveIn(BaseModel):
    grn_no: Optional[str] = None
    lines: list[ReceiveLineIn]


def _po_status_from_receipts(session: Session, po_id: str) -> str:
    """Recompute a PO's receiving status from received-vs-ordered across ALL lines:
    every line fully received -> RECEIVED; some received -> PARTIALLY_RECEIVED;
    none -> PARTIALLY_RECEIVED is not reached (we are called only after a receive)."""
    lines = _po_lines(session, po_id)
    received = _received_by_line(session, po_id)
    fully = all(received.get(ln.id, 0.0) >= ln.quantity for ln in lines)
    return "RECEIVED" if (lines and fully) else "PARTIALLY_RECEIVED"


@router.post("/purchase-orders/{po_id}/receive")
def receive_purchase_order(
    po_id: str,
    body: ReceiveIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Capture a goods receipt (GRN) against a PO. OFFICER/ADMIN only.

    Validates the PO is receivable, each line belongs to the PO, and the cumulative
    received quantity never exceeds the ordered quantity. Creates Receipt rows that
    share one grn_no (one per received line), recomputes the PO status
    (PARTIALLY_RECEIVED / RECEIVED), audits the transition, then (a) enqueues a BC
    receipt post via the existing integration outbox and (b) re-reads stock for the
    received items.

    STOCK: the operational systems (Kiwiplan/Accura) own the on-hand increment
    (CLAUDE.md §2 — this app DISPLAYS stock, it is never a competing source of
    truth). So we call stock_service.refresh_item to RE-READ from source rather than
    mutating stock_snapshots. In demo mode the source data is static, so the numbers
    won't visibly change after a receive — that is correct, not a bug."""
    _require_po_editor(user)
    po = _get_po(session, po_id)
    if po.status not in RECEIVABLE_STATES:
        raise _bad_transition(po.status, "receive")
    if not body.lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="At least one receive line is required")

    lines = {ln.id: ln for ln in _po_lines(session, po_id)}
    already = _received_by_line(session, po_id)

    # Validate every line up front (atomic: no Receipt rows written on a bad batch).
    # Accumulate within-batch quantities so two lines for the same po_line in one
    # GRN are summed against the ordered qty.
    batch: dict[str, float] = {}
    for rl in body.lines:
        po_line = lines.get(rl.po_line_id)
        if po_line is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PO line {rl.po_line_id} does not belong to this purchase order",
            )
        if rl.quantity <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Receipt quantity must be positive")
        batch[rl.po_line_id] = batch.get(rl.po_line_id, 0.0) + rl.quantity
        cumulative = already.get(rl.po_line_id, 0.0) + batch[rl.po_line_id]
        if cumulative > po_line.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Over-receipt on line {rl.po_line_id}: received {cumulative} "
                    f"exceeds ordered {po_line.quantity}"
                ),
            )

    grn_no = body.grn_no or _gen_grn()
    received_payload: list[dict] = []
    for rl in body.lines:
        po_line = lines[rl.po_line_id]
        session.add(Receipt(
            po_id=po_id, po_line_id=rl.po_line_id, item_id=po_line.item_id,
            grn_no=grn_no, quantity=rl.quantity,
        ))
        received_payload.append({
            "po_line_id": rl.po_line_id, "item_id": po_line.item_id,
            "quantity": rl.quantity, "location": rl.location,
        })
    session.commit()

    # Recompute + audit the PO receiving transition.
    prev = po.status
    po.status = _po_status_from_receipts(session, po_id)
    session.add(po)
    _record_event(
        session,
        entity_kind=ENTITY_KIND, entity_id=po.id,
        from_status=prev, to_status=po.status, event_type="RECEIVED", actor=user.email,
        detail={"grn_no": grn_no, "lines": received_payload},
    )
    session.commit()

    # (a) Enqueue the BC receipt post (reliable + idempotent via the outbox); drain
    # inline when configured, exactly like PO issue.
    enqueue_receipt(session, po, grn_no, received_payload)
    if settings.outbox_process_on_issue:
        process_outbox(session)

    # (b) Re-read stock for each received item from its source system (never write
    # competing stock truth). Guarded so a stock-read hiccup can't fail the receive.
    refreshed = _refresh_received_items(session, received_payload)

    session.refresh(po)
    detail = _detail(session, po)
    detail["grn_no"] = grn_no
    detail["stock_refreshed"] = refreshed
    return detail


def _refresh_received_items(session: Session, received_lines: list[dict]) -> list[str]:
    """Re-read stock from source for each received item (CLAUDE.md §2). Returns the
    SKUs refreshed. Never raises: a stock-read problem must not fail the receive."""
    skus: list[str] = []
    for item_id in {ln["item_id"] for ln in received_lines if ln.get("item_id")}:
        item = session.get(Item, item_id)
        if item is None:
            continue
        try:
            stock_service.refresh_item(session, item)
            skus.append(item.sku)
        except Exception:  # pragma: no cover - best-effort re-read
            log.exception("stock refresh after receipt failed item=%s", item_id)
    return skus


@router.get("/vendors")
def list_vendors(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return [
        {"id": v.id, "name": v.name, "email": v.email, "bc_vendor_no": v.bc_vendor_no}
        for v in session.exec(select(Vendor).order_by(Vendor.name)).all()
    ]
