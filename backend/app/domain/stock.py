"""Stock + dashboard API. All read endpoints require an authenticated user
(VIEWER can see). Refreshing the whole catalog is a heavier op gated to mutators."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user, require_mutator
from ..db import get_session
from ..gateway.models import Item
from . import stock_service

router = APIRouter(prefix="/api", tags=["stock"])


def _get_item(session: Session, sku: str) -> Item:
    item = session.exec(select(Item).where(Item.sku == sku)).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown SKU: {sku}")
    return item


@router.get("/dashboard")
def get_dashboard(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return stock_service.dashboard(session)


@router.get("/stock")
def search_stock(
    q: str = Query("", description="SKU or name fragment"),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return {"results": stock_service.search_items(session, q=q, limit=limit),
            "systems": stock_service.system_status()}


@router.get("/stock/{sku}")
def get_stock(
    sku: str,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    return stock_service.unified_view(session, _get_item(session, sku))


@router.post("/stock/{sku}/refresh")
def refresh_stock(
    sku: str,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    """On-demand 'refresh this material' — re-read its stock from the systems."""
    item = _get_item(session, sku)
    stock_service.refresh_item(session, item)
    return stock_service.unified_view(session, item)


@router.post("/stock-refresh-all")
def refresh_all(
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(require_mutator),
):
    count = stock_service.refresh_all(session)
    return {"refreshed": count}
