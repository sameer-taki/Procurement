"""Shipment tracking (SOP step 8) — the in-transit record per PO.

Replaces the manual Visy_Order_Details workbook: once the vendor's order
confirmation arrives, the officer records vessel / ETD / ETA / rolls / weight /
FCL count against the PO and updates it as the shipment moves. Open shipments
surface as next-arrival ETAs on the Order Page, and keeping the record current
is what stops in-transit volume being double-ordered (SOP §9).

Every change is audited as an OrderEvent on the PO (entity_kind PURCHASE_ORDER),
so the PO timeline reads order -> confirmation -> vessel -> receipt end to end.
"""
import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway.models import (
    OrderEvent,
    PurchaseOrder,
    Shipment,
    Vendor,
)

router = APIRouter(prefix="/api", tags=["shipments"])

# Same mutator gate as the PO workflow the shipments hang off.
SHIPMENT_EDITOR_ROLES = {"OFFICER", "ADMIN"}

PO_ENTITY_KIND = "PURCHASE_ORDER"

SHIPMENT_STATUSES = ("CONFIRMED", "ON_WATER", "ARRIVED", "RECEIVED", "CANCELLED")


def _require_editor(user: CurrentUser) -> None:
    if user.role_code not in SHIPMENT_EDITOR_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(SHIPMENT_EDITOR_ROLES))}",
        )


class ShipmentIn(BaseModel):
    vessel: Optional[str] = Field(default=None, max_length=120)
    etd: Optional[date] = None
    eta: Optional[date] = None
    rolls: Optional[int] = Field(default=None, ge=0)
    weight_kg: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    fcl_count: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


def _validate_status(value: Optional[str]) -> None:
    if value is not None and value not in SHIPMENT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bad status '{value}': expected one of {', '.join(SHIPMENT_STATUSES)}",
        )


def _out(s: Shipment, po: Optional[PurchaseOrder] = None,
         vendor: Optional[Vendor] = None) -> dict:
    out = {
        "id": s.id,
        "po_id": s.po_id,
        "vessel": s.vessel,
        "etd": s.etd.isoformat() if s.etd else None,
        "eta": s.eta.isoformat() if s.eta else None,
        "rolls": s.rolls,
        "weight_kg": s.weight_kg,
        "fcl_count": s.fcl_count,
        "status": s.status,
        "notes": s.notes,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }
    if po is not None:
        out["po_number"] = po.number
        out["po_status"] = po.status
        out["vendor"] = vendor.name if vendor else None
    return out


def _audit(session: Session, po: PurchaseOrder, event_type: str,
           actor: str, detail: dict) -> None:
    session.add(OrderEvent(
        entity_kind=PO_ENTITY_KIND,
        entity_id=po.id,
        from_status=po.status,
        to_status=po.status,
        event_type=event_type,
        actor=actor,
        detail_json=json.dumps(detail),
    ))


def po_shipments(session: Session, po_id: str) -> list[dict]:
    """Serialised shipments for one PO — used by the PO detail payload."""
    rows = session.exec(
        select(Shipment).where(Shipment.po_id == po_id).order_by(Shipment.created_at)
    ).all()
    return [_out(s) for s in rows]


@router.get("/shipments")
def shipping_schedule(
    status_filter: Optional[str] = Query(None, alias="status"),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """The Shipping Schedule: every tracked shipment with its PO + vendor, open
    (undelivered) first by ETA so the next arrivals lead."""
    stmt = select(Shipment)
    if status_filter:
        _validate_status(status_filter)
        stmt = stmt.where(Shipment.status == status_filter)
    shipments = session.exec(stmt).all()
    pos = {
        po.id: po
        for po in session.exec(
            select(PurchaseOrder).where(
                PurchaseOrder.id.in_({s.po_id for s in shipments})
            )
        ).all()
    } if shipments else {}
    vendors = {v.id: v for v in session.exec(select(Vendor)).all()}

    def sort_key(s: Shipment):
        open_ = s.status in ("CONFIRMED", "ON_WATER")
        return (not open_, s.eta or date.max, s.created_at)

    return [
        _out(s, pos.get(s.po_id),
             vendors.get(pos[s.po_id].vendor_id) if s.po_id in pos else None)
        for s in sorted(shipments, key=sort_key)
    ]


@router.post("/purchase-orders/{po_id}/shipments",
             status_code=status.HTTP_201_CREATED)
def create_shipment(
    po_id: str,
    body: ShipmentIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown purchase order")
    if po.status in ("DRAFT", "CANCELLED", "CLOSED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot record a shipment for a purchase order in state {po.status}",
        )
    _validate_status(body.status)
    shipment = Shipment(
        po_id=po.id,
        vessel=body.vessel,
        etd=body.etd,
        eta=body.eta,
        rolls=body.rolls,
        weight_kg=body.weight_kg,
        fcl_count=body.fcl_count,
        status=body.status or "CONFIRMED",
        notes=body.notes,
    )
    session.add(shipment)
    _audit(session, po, "SHIPMENT_RECORDED", user.email, {
        "shipment_id": shipment.id, "vessel": body.vessel,
        "etd": body.etd.isoformat() if body.etd else None,
        "eta": body.eta.isoformat() if body.eta else None,
        "fcl_count": body.fcl_count, "weight_kg": body.weight_kg,
    })
    session.commit()
    session.refresh(shipment)
    return _out(shipment)


@router.patch("/shipments/{shipment_id}")
def update_shipment(
    shipment_id: str,
    body: ShipmentIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Partial update: only the fields present in the payload change (recording a
    vessel later, moving status along, correcting an ETA)."""
    _require_editor(user)
    shipment = session.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown shipment")
    po = session.get(PurchaseOrder, shipment.po_id)
    _validate_status(body.status)

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No fields to update")
    for field, value in changes.items():
        setattr(shipment, field, value)
    shipment.updated_at = datetime.utcnow()
    session.add(shipment)
    if po is not None:
        _audit(session, po, "SHIPMENT_UPDATED", user.email, {
            "shipment_id": shipment.id,
            "changes": {
                k: (v.isoformat() if isinstance(v, date) else v)
                for k, v in changes.items()
            },
        })
    session.commit()
    session.refresh(shipment)
    return _out(shipment)
