"""Requisitions + tiered approval. All endpoints under /api.

The gateway is the only writer of canonical state (CLAUDE.md §2): this app owns
the requisition/approval workflow and decides every status transition. Each
transition writes an OrderEvent (entity_kind='REQUISITION') so the trail is fully
audited (Phase 2 DoD).

States: DRAFT -> SUBMITTED -> IN_APPROVAL -> APPROVED | REJECTED ;
CANCELLED from any pre-approval state. (APPROVED -> CLOSED is Phase 3.)

Estimated amount = sum(line.quantity * (item.sales_price or 0)). It is an
ESTIMATE — BC selling price is the only price available until vendor_prices land
in Phase 3 — so it is always labelled "estimated".
"""
import json
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway.models import (
    Item, OrderEvent, PurchaseOrder, Requisition, RequisitionLine, VendorPrice,
)
from .approvals import APPROVER_ROLES, can_approve, required_tier

router = APIRouter(prefix="/api", tags=["requisitions"])

ENTITY_KIND = "REQUISITION"

# Who may run the requester-side workflow (create/update/submit/cancel).
EDITOR_ROLES = {"REQUESTER", "OFFICER", "ADMIN"}

# Pre-approval states from which a requisition can be cancelled.
CANCELLABLE = {"DRAFT", "SUBMITTED", "IN_APPROVAL"}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class LineIn(BaseModel):
    sku: str
    # Reject negative / zero / NaN / inf at the boundary: a non-finite qty would
    # poison the estimated amount (NaN routing, inf overflow) and a <=0 qty is a
    # meaningless requisition line. Mirrors the bom explode guard.
    quantity: float = Field(gt=0, allow_inf_nan=False)
    needed_by: Optional[date] = None


class RequisitionIn(BaseModel):
    cost_center: Optional[str] = Field(default=None, max_length=60)
    lines: list[LineIn] = Field(min_length=1, max_length=500)


class RequisitionUpdate(BaseModel):
    cost_center: Optional[str] = Field(default=None, max_length=60)
    lines: Optional[list[LineIn]] = Field(default=None, max_length=500)


class RejectIn(BaseModel):
    reason: str = Field(max_length=1000)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gen_number() -> str:
    # 12 hex chars (48 bits) of randomness per day makes collisions vanishingly
    # unlikely; create_requisition still retries on the rare unique-constraint hit.
    return f"REQ-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:12]}"


def _get_req(session: Session, req_id: str) -> Requisition:
    req = session.get(Requisition, req_id)
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown requisition")
    return req


def _lines(session: Session, req_id: str) -> list[RequisitionLine]:
    return session.exec(
        select(RequisitionLine).where(RequisitionLine.requisition_id == req_id)
    ).all()


def _lines_by_req(session: Session, req_ids: list[str]) -> dict[str, list[RequisitionLine]]:
    """Batch-load lines for many requisitions in one query, grouped by requisition_id.

    Avoids the per-requisition SELECT that the list/queue endpoints used to fire
    (the N+1 in the Phase 2 hardening notes).
    """
    grouped: dict[str, list[RequisitionLine]] = {rid: [] for rid in req_ids}
    if not req_ids:
        return grouped
    rows = session.exec(
        select(RequisitionLine).where(RequisitionLine.requisition_id.in_(req_ids))
    ).all()
    for ln in rows:
        grouped.setdefault(ln.requisition_id, []).append(ln)
    return grouped


def _price_map(session: Session, lines: list[RequisitionLine]) -> dict[str, float]:
    """{item_id: best unit-price estimate} for approval routing.

    sales_price is BC's *sell* price and is null for purchased materials (paper
    is bought, not sold) — using `sales_price or 0` alone made a 100-tonne paper
    requisition estimate at FJD 0 and route to the lowest approval tier, i.e.
    anyone could approve it. So fall back per item: sales_price, else the
    cheapest vendor price (what we'd actually pay — the same signal PO creation
    uses), else the rolled-up std_cost, else 0. Two batched queries, no N+1."""
    item_ids = {ln.item_id for ln in lines}
    if not item_ids:
        return {}
    items = session.exec(select(Item).where(Item.id.in_(item_ids))).all()
    # Cheapest vendor price per item, in one query.
    cheapest: dict[str, float] = {}
    for vp in session.exec(
        select(VendorPrice).where(VendorPrice.item_id.in_(item_ids))
    ).all():
        if vp.item_id not in cheapest or vp.price < cheapest[vp.item_id]:
            cheapest[vp.item_id] = vp.price
    out: dict[str, float] = {}
    for it in items:
        if it.sales_price:
            out[it.id] = it.sales_price
        elif it.id in cheapest:
            out[it.id] = cheapest[it.id]
        else:
            out[it.id] = it.std_cost or 0.0
    return out


def _item_for_sku(session: Session, sku: str) -> Item:
    item = session.exec(select(Item).where(Item.sku == sku)).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown SKU: {sku}")
    return item


def _amount_from(lines: list[RequisitionLine], prices: dict[str, float]) -> float:
    """Sum of quantity * (item.sales_price or 0), using a pre-loaded price map."""
    return sum(ln.quantity * prices.get(ln.item_id, 0.0) for ln in lines)


def _estimated_amount(session: Session, lines: list[RequisitionLine]) -> float:
    """Sum of quantity * (item.sales_price or 0). An estimate from BC list price.

    Loads the per-item prices in a single query for the given lines.
    """
    return _amount_from(lines, _price_map(session, lines))


def _record_event(
    session: Session,
    req: Requisition,
    *,
    from_status: Optional[str],
    to_status: Optional[str],
    event_type: str,
    actor: str,
    detail: Optional[dict] = None,
) -> None:
    """Audit every status transition into order_events."""
    session.add(OrderEvent(
        entity_kind=ENTITY_KIND,
        entity_id=req.id,
        from_status=from_status,
        to_status=to_status,
        event_type=event_type,
        actor=actor,
        detail_json=json.dumps(detail) if detail is not None else None,
    ))


def _events(session: Session, req_id: str) -> list[OrderEvent]:
    return session.exec(
        select(OrderEvent)
        .where(OrderEvent.entity_kind == ENTITY_KIND, OrderEvent.entity_id == req_id)
        .order_by(OrderEvent.id)
    ).all()


def _require_editor(user: CurrentUser) -> None:
    if user.role_code not in EDITOR_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(EDITOR_ROLES))}",
        )


def _require_owner_or_admin(user: CurrentUser, req: Requisition) -> None:
    """Editing/submitting/cancelling is the requester's own action; OFFICER/ADMIN
    may act on any requisition (ADMIN always; OFFICER as the procurement desk)."""
    if user.role_code == "ADMIN":
        return
    if req.requester == user.email:
        return
    if user.role_code == "OFFICER":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the requester, an OFFICER, or an ADMIN may act on this requisition",
    )


def _bad_transition(current: str, action: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot {action} a requisition in state {current}",
    )


# --------------------------------------------------------------------------- #
# Serialisers
# --------------------------------------------------------------------------- #
def _summary(req: Requisition, lines: list[RequisitionLine], amount: float) -> dict:
    """Serialise a queue/list row from already-loaded lines + pre-computed amount."""
    return {
        "id": req.id,
        "number": req.number,
        "requester": req.requester,
        "status": req.status,
        "line_count": len(lines),
        "estimated_amount": amount,
        "created_at": req.created_at.isoformat(),
    }


def _detail(session: Session, req: Requisition) -> dict:
    lines = _lines(session, req.id)
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_({ln.item_id for ln in lines}))
        ).all()
    } if lines else {}
    # Use the SAME price basis approval routing does (sales_price, else vendor
    # price, else std_cost) so the displayed estimate can never disagree with the
    # tier a requisition actually routes to — otherwise a null-sales_price paper
    # req would show FJD 0 here while routing off its real vendor cost.
    prices = _price_map(session, lines)
    amount = _amount_from(lines, prices)
    line_out = []
    for ln in lines:
        item = items.get(ln.item_id)
        unit_price = prices.get(ln.item_id, 0.0)
        line_out.append({
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "quantity": ln.quantity,
            "unit_price": unit_price,
            "line_total": ln.quantity * unit_price,
            "needed_by": ln.needed_by.isoformat() if ln.needed_by else None,
        })
    events = [{
        "from_status": e.from_status,
        "to_status": e.to_status,
        "event_type": e.event_type,
        "actor": e.actor,
        "detail": json.loads(e.detail_json) if e.detail_json else None,
        "occurred_at": e.occurred_at.isoformat(),
    } for e in _events(session, req.id)]
    return {
        "id": req.id,
        "number": req.number,
        "requester": req.requester,
        "status": req.status,
        "cost_center": req.cost_center,
        "created_at": req.created_at.isoformat(),
        "estimated_amount": amount,
        "amount_label": "estimated",
        "required_tier": required_tier(amount),
        "lines": line_out,
        "events": events,
    }


def _replace_lines(session: Session, req: Requisition, lines: list[LineIn]) -> None:
    for old in _lines(session, req.id):
        session.delete(old)
    for ln in lines:
        item = _item_for_sku(session, ln.sku)
        session.add(RequisitionLine(
            requisition_id=req.id,
            item_id=item.id,
            quantity=ln.quantity,
            needed_by=ln.needed_by,
        ))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/requisitions", status_code=status.HTTP_201_CREATED)
def create_requisition(
    body: RequisitionIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    if not body.lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one line is required")

    # Guarantee a unique requisition number: retry on the (rare) number-collision
    # IntegrityError with a freshly generated number rather than surfacing a 500.
    last_error: Optional[IntegrityError] = None
    for _ in range(5):
        req = Requisition(
            number=_gen_number(),
            requester=user.email,
            status="DRAFT",
            source="manual",
            cost_center=body.cost_center,
        )
        session.add(req)
        _replace_lines(session, req, body.lines)
        _record_event(
            session, req,
            from_status=None, to_status="DRAFT", event_type="CREATED", actor=user.email,
            detail={"cost_center": body.cost_center, "line_count": len(body.lines)},
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            last_error = exc
            continue
        session.refresh(req)
        return _detail(session, req)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not allocate a unique requisition number",
    ) from last_error


@router.get("/requisitions")
def list_requisitions(
    status_filter: Optional[str] = Query(None, alias="status"),
    mine: bool = Query(False),
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(Requisition)
    if status_filter:
        stmt = stmt.where(Requisition.status == status_filter)
    if mine:
        stmt = stmt.where(Requisition.requester == user.email)
    reqs = session.exec(stmt.order_by(Requisition.created_at.desc())).all()

    # Batch-load all lines + all item prices in two queries, then compute each
    # amount once (no per-requisition / per-line round-trips).
    lines_by_req = _lines_by_req(session, [r.id for r in reqs])
    all_lines = [ln for lns in lines_by_req.values() for ln in lns]
    prices = _price_map(session, all_lines)
    return [
        _summary(r, lines_by_req[r.id], _amount_from(lines_by_req[r.id], prices))
        for r in reqs
    ]


@router.get("/approvals")
def approvals_waiting(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """'Waiting on me': IN_APPROVAL requisitions whose estimated amount the
    current user may approve, given their role limit."""
    reqs = session.exec(
        select(Requisition).where(Requisition.status == "IN_APPROVAL")
        .order_by(Requisition.created_at.desc())
    ).all()

    # Batch-load lines + prices once, then reuse each computed amount for both the
    # limit check and the summary (no recompute, no per-requisition queries).
    lines_by_req = _lines_by_req(session, [r.id for r in reqs])
    all_lines = [ln for lns in lines_by_req.values() for ln in lns]
    prices = _price_map(session, all_lines)
    out = []
    for r in reqs:
        lines = lines_by_req[r.id]
        amount = _amount_from(lines, prices)
        if can_approve(user.role_code, user.approval_limit, amount):
            out.append(_summary(r, lines, amount))
    return out


@router.get("/requisitions/{req_id}")
def get_requisition(
    req_id: str,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return _detail(session, _get_req(session, req_id))


@router.put("/requisitions/{req_id}")
def update_requisition(
    req_id: str,
    body: RequisitionUpdate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    req = _get_req(session, req_id)
    if req.status != "DRAFT":
        raise _bad_transition(req.status, "edit")
    _require_owner_or_admin(user, req)

    if body.cost_center is not None:
        req.cost_center = body.cost_center
    if body.lines is not None:
        if not body.lines:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one line is required")
        _replace_lines(session, req, body.lines)
    session.add(req)
    _record_event(
        session, req,
        from_status="DRAFT", to_status="DRAFT", event_type="UPDATED", actor=user.email,
        detail={"cost_center": req.cost_center},
    )
    session.commit()
    session.refresh(req)
    return _detail(session, req)


@router.post("/requisitions/{req_id}/submit")
def submit_requisition(
    req_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    req = _get_req(session, req_id)
    if req.status != "DRAFT":
        raise _bad_transition(req.status, "submit")
    _require_owner_or_admin(user, req)

    amount = _estimated_amount(session, _lines(session, req.id))
    req.status = "IN_APPROVAL"
    session.add(req)
    _record_event(
        session, req,
        from_status="DRAFT", to_status="IN_APPROVAL", event_type="SUBMITTED", actor=user.email,
        detail={"estimated_amount": amount, "required_tier": required_tier(amount)},
    )
    session.commit()
    session.refresh(req)
    return _detail(session, req)


@router.post("/requisitions/{req_id}/approve")
def approve_requisition(
    req_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    req = _get_req(session, req_id)
    if req.status != "IN_APPROVAL":
        raise _bad_transition(req.status, "approve")

    # Separation of duties: an approver may not sign off on their own requisition
    # (ADMIN excepted, as the break-glass role).
    if req.requester == user.email and user.role_code != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot approve your own requisition",
        )

    amount = _estimated_amount(session, _lines(session, req.id))
    if not can_approve(user.role_code, user.approval_limit, amount):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your approval limit does not cover the estimated amount "
                f"{amount:.2f}; requires {required_tier(amount)['role']}"
            ),
        )
    req.status = "APPROVED"
    session.add(req)
    _record_event(
        session, req,
        from_status="IN_APPROVAL", to_status="APPROVED", event_type="APPROVED", actor=user.email,
        detail={"estimated_amount": amount, "approver_limit": user.approval_limit},
    )
    session.commit()
    session.refresh(req)
    return _detail(session, req)


@router.post("/requisitions/{req_id}/reject")
def reject_requisition(
    req_id: str,
    body: RejectIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    if user.role_code not in APPROVER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(APPROVER_ROLES))}",
        )
    req = _get_req(session, req_id)
    if req.status != "IN_APPROVAL":
        raise _bad_transition(req.status, "reject")

    req.status = "REJECTED"
    session.add(req)
    _record_event(
        session, req,
        from_status="IN_APPROVAL", to_status="REJECTED", event_type="REJECTED", actor=user.email,
        detail={"reason": body.reason},
    )
    session.commit()
    session.refresh(req)
    return _detail(session, req)


def _existing_pos_for_req(session: Session, req_id: str) -> bool:
    """True iff any PurchaseOrder was created from this requisition (used to gate
    the APPROVED->CANCELLED exit; local to avoid importing purchasing)."""
    return session.exec(
        select(PurchaseOrder).where(PurchaseOrder.requisition_id == req_id)
    ).first() is not None


@router.post("/requisitions/{req_id}/cancel")
def cancel_requisition(
    req_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    req = _get_req(session, req_id)
    # An APPROVED requisition can still be cancelled IFF no PO has been created
    # from it yet — otherwise it is a dead end: OPEN_REQ_STATES (planning) counts
    # APPROVED as "in flight" and blocks the next coverage run, while create-po
    # would be the only exit. Once POs exist, cancel the POs instead.
    approved_no_pos = (
        req.status == "APPROVED"
        and not _existing_pos_for_req(session, req.id)
    )
    if req.status not in CANCELLABLE and not approved_no_pos:
        raise _bad_transition(req.status, "cancel")
    # Cancellation is the requester's own action (or ADMIN).
    if user.role_code != "ADMIN" and req.requester != user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the requester or an ADMIN may cancel this requisition",
        )

    prev = req.status
    req.status = "CANCELLED"
    session.add(req)
    _record_event(
        session, req,
        from_status=prev, to_status="CANCELLED", event_type="CANCELLED", actor=user.email,
    )
    session.commit()
    session.refresh(req)
    return _detail(session, req)
