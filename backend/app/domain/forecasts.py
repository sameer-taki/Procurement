"""Customer forecasts (SOP step 1) — the planning run's demand input.

Sales/Customer Service submit the rolling forecast per finished-good item in
CARTONS per calendar month. One row per (customer, item, period); resubmitting a
figure replaces the previous one (the forecast is a statement of current truth,
not a ledger). Step 2's carton->KG conversion happens in domain/planning.py by
exploding these through the BOMs — this module only owns capture + review.
"""
from datetime import datetime
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway.models import Forecast, Item

router = APIRouter(prefix="/api", tags=["forecasts"])

# Who may enter/replace forecast figures. Sales/CS map to OFFICER in practice;
# mirrors the other mutator gates (VIEWER/REQUESTER/bare APPROVER read only).
FORECAST_EDITOR_ROLES = {"OFFICER", "ADMIN"}

# \Z, not $: '$' would accept a trailing newline, and a period of "2026-07\n"
# passes validation but never matches the planning window's exact-string query,
# so the forecast would silently vanish from the Order Page.
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\Z")

# Upper bound on one upsert batch: a full year for every customer/item pair a
# planner would realistically paste in one go; oversized payloads -> 422.
MAX_FORECAST_LINES = 1000


def _require_editor(user: CurrentUser) -> None:
    if user.role_code not in FORECAST_EDITOR_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(FORECAST_EDITOR_ROLES))}",
        )


class ForecastLineIn(BaseModel):
    customer: str = Field(min_length=1, max_length=120)
    sku: str
    period: str
    # 0 is meaningful (an explicit 'no demand this month' overwrite); negative or
    # non-finite figures are rejected at the boundary.
    qty_cartons: float = Field(ge=0, allow_inf_nan=False)


class ForecastUpsertIn(BaseModel):
    lines: list[ForecastLineIn] = Field(min_length=1, max_length=MAX_FORECAST_LINES)


def _out(f: Forecast, item: Optional[Item]) -> dict:
    return {
        "id": f.id,
        "customer": f.customer,
        "sku": item.sku if item else None,
        "name": item.name if item else None,
        "period": f.period,
        "qty_cartons": f.qty_cartons,
        "updated_by": f.updated_by,
        "updated_at": f.updated_at.isoformat(),
    }


@router.get("/forecasts")
def list_forecasts(
    customer: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    sku: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    stmt = select(Forecast)
    if customer:
        stmt = stmt.where(Forecast.customer == customer)
    if period:
        stmt = stmt.where(Forecast.period == period)
    if sku:
        item = session.exec(select(Item).where(Item.sku == sku)).first()
        if item is None:
            return []
        stmt = stmt.where(Forecast.item_id == item.id)
    forecasts = session.exec(
        stmt.order_by(Forecast.period, Forecast.customer)
    ).all()
    items = {
        it.id: it
        for it in session.exec(
            select(Item).where(Item.id.in_({f.item_id for f in forecasts}))
        ).all()
    } if forecasts else {}
    return [_out(f, items.get(f.item_id)) for f in forecasts]


@router.put("/forecasts")
def upsert_forecasts(
    body: ForecastUpsertIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Bulk upsert: each (customer, sku, period) replaces any existing figure.
    All lines validate before anything is written (atomic batch).

    Check-then-write is raced by a concurrent PUT of the same new key (two sales
    users pasting the same customer's sheet): the loser's INSERT hits
    uq_forecasts_customer_item_period. One retry re-reads and lands as an
    UPDATE — last writer wins, which is the endpoint's contract anyway."""
    _require_editor(user)

    resolved: list[tuple[ForecastLineIn, Item]] = []
    for ln in body.lines:
        if not PERIOD_RE.match(ln.period):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Bad period '{ln.period}': expected YYYY-MM",
            )
        item = session.exec(select(Item).where(Item.sku == ln.sku)).first()
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown SKU: {ln.sku}"
            )
        resolved.append((ln, item))

    now = datetime.utcnow()
    last_error: Optional[IntegrityError] = None
    for _ in range(2):
        written = 0
        for ln, item in resolved:
            existing = session.exec(
                select(Forecast).where(
                    Forecast.customer == ln.customer,
                    Forecast.item_id == item.id,
                    Forecast.period == ln.period,
                )
            ).first()
            if existing is None:
                existing = Forecast(
                    customer=ln.customer, item_id=item.id, period=ln.period,
                    qty_cartons=ln.qty_cartons,
                )
            existing.qty_cartons = ln.qty_cartons
            existing.updated_by = user.email
            existing.updated_at = now
            session.add(existing)
            written += 1
        try:
            session.commit()
        except IntegrityError as exc:   # lost the upsert race; retry as UPDATEs
            session.rollback()
            last_error = exc
            continue
        return {"written": written}
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A concurrent forecast update is in progress; retry shortly",
    ) from last_error


@router.delete("/forecasts/{forecast_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_forecast(
    forecast_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_editor(user)
    f = session.get(Forecast, forecast_id)
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Unknown forecast")
    session.delete(f)
    session.commit()
    return None
