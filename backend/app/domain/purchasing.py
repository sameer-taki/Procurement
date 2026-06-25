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
    Requisition,
    RequisitionLine,
    Vendor,
    VendorPrice,
)

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
BC_SYSTEM = "BC"
BC_PO_TYPE = "PURCHASE_ORDER"
MAX_ATTEMPTS = 5

# Who may run the PO workflow (create/issue). Mirrors stock's mutator gate.
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


def _detail(session: Session, po: PurchaseOrder) -> dict:
    lines = _po_lines(session, po.id)
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_({ln.item_id for ln in lines}))
        ).all()
    } if lines else {}
    line_out = []
    for ln in lines:
        item = items.get(ln.item_id)
        line_out.append({
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "quantity": ln.quantity,
            "unit_price": ln.unit_price,
            "line_total": ln.quantity * ln.unit_price,
        })

    vendor = session.get(Vendor, po.vendor_id)
    ref = _bc_ref(session, po.id)
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
        "email_status": email_status,
        "created_at": po.created_at.isoformat(),
        "lines": line_out,
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
    """Process PENDING BC create_purchase_order rows. Reliable + idempotent.

    Concurrency-safe (CLAUDE.md Phase 3 DoD: NEVER double-post). For each PENDING
    row with attempts < max_attempts:
      * CLAIM the row atomically (PENDING -> SENDING via a conditional UPDATE). A
        concurrent worker that already claimed it loses the race and skips it, so
        only one caller posts a given row to BC.
      * IDEMPOTENCY GUARD: if an ExternalRef already exists for this PO, the PO is
        already posted -> mark the row SENT and DO NOT call BC again.
      * else call bc.create_purchase_order(payload); on success write the
        ExternalRef (external_id=bc_po_no), mark SENT, set PO ACKNOWLEDGED, record
        an OrderEvent, then notify the vendor and record the email status. A unique
        constraint on the crosswalk makes a duplicate insert raise IntegrityError,
        which we treat as 'already posted' (defence in depth behind the claim).
      * on exception: attempts += 1, last_error set; the row returns to PENDING for
        a later retry UNLESS attempts now reaches max_attempts, in which case it is
        marked FAILED (terminal, operator-visible) with a BC_POST_FAILED event.

    Running this twice (sequentially or concurrently) yields exactly one BC post +
    one ExternalRef.
    """
    posted = skipped = failed = 0
    rows = session.exec(
        select(IntegrationOutbox).where(
            IntegrationOutbox.target == OUTBOX_TARGET,
            IntegrationOutbox.action == OUTBOX_ACTION,
            IntegrationOutbox.status == "PENDING",
        ).order_by(IntegrationOutbox.id)
    ).all()

    for row in rows:
        # Already exhausted but still PENDING (e.g. legacy data): retire it now.
        if row.attempts >= max_attempts:
            _mark_failed(session, row, "max attempts reached")
            failed += 1
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
            failed += 1
            continue

        po_id = payload.get("po_id")
        po = session.get(PurchaseOrder, po_id) if po_id else None
        if po is None:
            _record_attempt_failure(session, row, po_id, f"unknown po_id {po_id}",
                                    max_attempts)
            failed += 1
            continue

        # IDEMPOTENCY GUARD — already posted? Mark SENT, never call BC again.
        ref = _bc_ref(session, po.id)
        if ref is not None:
            row.status = "SENT"
            session.add(row)
            session.commit()
            skipped += 1
            continue

        try:
            bc_po_no = bc.create_purchase_order(payload)
        except Exception as exc:  # back to PENDING for a retry (or FAILED if maxed)
            _record_attempt_failure(session, row, po.id, str(exc), max_attempts)
            failed += 1
            continue

        # Success: write the crosswalk FIRST (the idempotency anchor), then flip
        # the outbox + PO state in the same transaction. A unique-constraint
        # collision here means a racing worker already posted -> treat as 'already
        # posted' rather than double-posting.
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
            # Lost a crosswalk race: the PO is already posted. Reconcile this row to
            # SENT without re-posting and move on.
            session.rollback()
            session.refresh(row)
            row.status = "SENT"
            session.add(row)
            session.commit()
            skipped += 1
            continue

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
        posted += 1

    return {"posted": posted, "skipped": skipped, "failed": failed}


def _mark_failed(session: Session, row: IntegrationOutbox, error: str) -> None:
    """Move an outbox row to the terminal FAILED state and audit the PO."""
    row.status = "FAILED"
    if error:
        row.last_error = error
    session.add(row)
    po_id = row.entity_ref
    if not po_id:
        try:
            po_id = json.loads(row.request_json).get("po_id")
        except (ValueError, TypeError):
            po_id = None
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


@router.get("/vendors")
def list_vendors(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return [
        {"id": v.id, "name": v.name, "email": v.email, "bc_vendor_no": v.bc_vendor_no}
        for v in session.exec(select(Vendor).order_by(Vendor.name)).all()
    ]
