"""Phase 4 — BOM + explosion service: the order->procurement bridge.

REUSES the pure engine in `app.gateway.bom` AS-IS (CLAUDE.md Phase 4): this module
is the thin service layer that wires `explode` / `net_requirements` / `round_to_moq`
/ `explode_and_net` to the canonical tables via three plain callables:

  bom_of(item_id) -> (yield_qty, [BomNode(component=<component item_id>, ...)]) | None
      Read from the ACTIVE BomHeader for parent_item_id + its BomLines.
      None => the item has no active BOM (a purchased leaf), so recursion stops.
  stock(item_id) -> (on_hand, allocated, on_order)
      Aggregated from stock_snapshots (0,0,0 when none). The engine derives
      available = on_hand - allocated + on_order itself.
  moq(item_id)   -> the chosen-vendor MOQ
      The moq of the cheapest vendor_price for the item (tie-break lower
      lead_time_days), mirroring the Phase 3 vendor selection. None/0 => no rounding.

The engine keys everything by item_id; the endpoints map those ids back to sku/name.

The gateway is the only writer of canonical state (CLAUDE.md §2). A demand signal
never writes stock or BOM truth — it only emits an app-owned DRAFT requisition
(source='demand') for the shortages, which then flows into the Phase 2 approval
lifecycle. We REUSE the Phase 2 requisition service (number scheme + audit event)
rather than forking it.
"""
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth.deps import CurrentUser, get_current_user
from ..db import get_session
from ..gateway.bom import (
    BomNode,
    explode,
    explode_and_net,
    net_requirements,
    round_to_moq,
)
from ..gateway.models import (
    BomHeader,
    BomLine,
    BomOwner,
    Item,
    OrderEvent,
    Requisition,
    RequisitionLine,
    StockSnapshot,
    Vendor,
    VendorPrice,
)
from . import requisitions as req_service
from .purchasing import _choose_vendor_price

# Upper bound on input lines for the (any-authed) explode + suggest entry points.
# Comfortably exceeds any realistic production order; oversized payloads -> 422
# rather than unbounded per-line DB work + explosion on a single request.
MAX_EXPLODE_LINES = 500

router = APIRouter(prefix="/api", tags=["bom"])

# Who may turn a demand signal into a (mutating) suggested requisition. Mirrors the
# stock/PO mutator gate: OFFICER/ADMIN only (VIEWER/REQUESTER -> 403).
SUGGEST_ROLES = {"OFFICER", "ADMIN"}

# OrderEvent.entity_kind for the requisition audit row (matches Phase 2).
REQ_ENTITY_KIND = "REQUISITION"


# --------------------------------------------------------------------------- #
# The three DB callables wired into the engine
# --------------------------------------------------------------------------- #
def _active_header(session: Session, parent_item_id: str) -> Optional[BomHeader]:
    """The single ACTIVE BomHeader for a parent, highest version wins.

    The 'exactly one ACTIVE header per parent' invariant holds for the demo seed,
    but live BC/Kiwiplan/Accura mirroring could land more than one ACTIVE header
    (e.g. a version bump that fails to mark the prior OBSOLETE). Ordering by
    version desc makes the pick deterministic instead of an arbitrary `.first()`.
    """
    return session.exec(
        select(BomHeader)
        .where(
            BomHeader.parent_item_id == parent_item_id,
            BomHeader.status == "ACTIVE",
        )
        .order_by(BomHeader.version.desc())
    ).first()


def _cheapest_vendor_price(
    session: Session, item_id: str
) -> Optional[VendorPrice]:
    """The chosen VendorPrice for an item: cheapest, tie-break lower lead time.

    Single source of truth for vendor selection in this module, delegating the
    tie-break to the Phase 3 helper (purchasing._choose_vendor_price) so moq() and
    vendor() can never disagree and stay aligned with Phase 3 purchasing.
    Returns None when the item has no vendor price.
    """
    prices = session.exec(
        select(VendorPrice).where(VendorPrice.item_id == item_id)
    ).all()
    if not prices:
        return None
    return _choose_vendor_price(prices)


def make_bom_of(session: Session) -> Callable[[str], Optional[tuple]]:
    """bom_of(item_id) -> (yield_qty, [BomNode]) | None.

    Reads the ACTIVE BomHeader for the parent item and its lines. BomNode.component
    is the COMPONENT's item_id so the engine's recursion resolves down the tree.
    Returns None when there is no active BOM (the item is a purchased leaf).
    """
    def _bom_of(item_id: str):
        header = _active_header(session, item_id)
        if header is None:
            return None
        lines = session.exec(
            select(BomLine).where(BomLine.bom_header_id == header.id)
            .order_by(BomLine.line_no)
        ).all()
        nodes = [
            BomNode(component=ln.component_id, qty_per=ln.qty_per,
                    scrap_pct=ln.scrap_pct)
            for ln in lines
        ]
        return (header.yield_qty or 1.0, nodes)

    return _bom_of


def make_stock(session: Session) -> Callable[[str], tuple]:
    """stock(item_id) -> (on_hand, allocated, on_order), summed over snapshots."""
    def _stock(item_id: str):
        snaps = session.exec(
            select(StockSnapshot).where(StockSnapshot.item_id == item_id)
        ).all()
        on_hand = sum(s.on_hand for s in snaps)
        allocated = sum(s.allocated for s in snaps)
        on_order = sum(s.on_order for s in snaps)
        return (on_hand, allocated, on_order)

    return _stock


def make_moq(session: Session) -> Callable[[str], Optional[float]]:
    """moq(item_id) -> the chosen-vendor MOQ (cheapest price, tie-break lower lead
    time), mirroring Phase 3 vendor selection. None when the item has no vendor
    price (=> the engine does no rounding)."""
    def _moq(item_id: str):
        vp = _cheapest_vendor_price(session, item_id)
        return vp.moq if vp else None

    return _moq


def chosen_vendor_name(session: Session, item_id: str) -> Optional[str]:
    """Name of the cheapest vendor (tie-break lower lead time) for an item, for the
    explode preview. None when the item has no vendor price."""
    vp = _cheapest_vendor_price(session, item_id)
    if vp is None:
        return None
    vendor = session.get(Vendor, vp.vendor_id)
    return vendor.name if vendor else None


# --------------------------------------------------------------------------- #
# SKU <-> item_id resolution
# --------------------------------------------------------------------------- #
def _item_for_sku(session: Session, sku: str) -> Item:
    item = session.exec(select(Item).where(Item.sku == sku)).first()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown SKU: {sku}"
        )
    return item


def _items_by_id(session: Session, item_ids) -> dict:
    if not item_ids:
        return {}
    return {
        it.id: it
        for it in session.exec(select(Item).where(Item.id.in_(set(item_ids)))).all()
    }


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class ExplodeLine(BaseModel):
    sku: str
    # Reject inf/nan/negative/zero at the request boundary (HIGH): a non-finite qty
    # would otherwise crash the handler (ceil(inf/m) -> OverflowError) or fail JSON
    # serialisation (NaN, allow_nan=False) as a 500; negative/zero is a meaningless
    # demand signal. allow_inf_nan=False + gt=0 turns all of these into a clean 422.
    qty: float = Field(gt=0, allow_inf_nan=False)


class ExplodeIn(BaseModel):
    lines: list[ExplodeLine] = Field(max_length=MAX_EXPLODE_LINES)


class SuggestIn(BaseModel):
    lines: list[ExplodeLine] = Field(max_length=MAX_EXPLODE_LINES)
    cost_center: Optional[str] = None


# --------------------------------------------------------------------------- #
# BOM tree (read-only view)
# --------------------------------------------------------------------------- #
def _bom_tree(session: Session, item: Item) -> Optional[dict]:
    """Recursive {sku,name,qty_per,scrap_pct,owner, components?} or None for a leaf.

    The top node has no qty_per/scrap_pct/owner (it is the parent itself); its
    `components` carry those per-line. Cycles raise ValueError from the same
    invariant the engine enforces (caught at the endpoint and surfaced as 409).
    """
    def _children(parent_id: str, seen: frozenset) -> Optional[list]:
        header = _active_header(session, parent_id)
        if header is None:
            return None
        if parent_id in seen:
            raise ValueError(f"BOM cycle detected at {parent_id}")
        seen = seen | {parent_id}
        lines = session.exec(
            select(BomLine).where(BomLine.bom_header_id == header.id)
            .order_by(BomLine.line_no)
        ).all()
        comp_items = _items_by_id(session, [ln.component_id for ln in lines])
        out = []
        for ln in lines:
            comp = comp_items.get(ln.component_id)
            node = {
                "sku": comp.sku if comp else None,
                "name": comp.name if comp else None,
                "qty_per": ln.qty_per,
                "scrap_pct": ln.scrap_pct,
                "owner": header.owner.value if hasattr(header.owner, "value")
                else header.owner,
            }
            sub = _children(ln.component_id, seen)
            if sub is not None:
                node["components"] = sub
            out.append(node)
        return out

    components = _children(item.id, frozenset())
    if components is None:
        return None
    header = _active_header(session, item.id)
    return {
        "sku": item.sku,
        "name": item.name,
        # Echoed so the spec editor can prefill (yield_qty=1.0 for carton specs).
        "yield_qty": header.yield_qty if header else 1.0,
        # The ACTIVE header version, for optimistic concurrency: an editor sends
        # it back as base_version so a stale save (another officer edited since)
        # is rejected instead of silently clobbering their lines.
        "version": header.version if header else 0,
        "components": components,
    }


# --------------------------------------------------------------------------- #
# Explosion preview (no writes)
# --------------------------------------------------------------------------- #
def _qty_list(items_by_id: dict, qty_by_id: dict) -> list[dict]:
    """Map an engine {item_id: qty} dict back to [{sku,name,qty}], sorted by sku."""
    out = []
    for item_id, qty in qty_by_id.items():
        item = items_by_id.get(item_id)
        out.append({
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "qty": qty,
        })
    return sorted(out, key=lambda r: (r["sku"] or ""))


def explode_preview(session: Session, lines: list[ExplodeLine]) -> dict:
    """Run the engine over (sku, qty) lines -> gross / net / suggested. NO writes.

    Raises HTTPException(404) for an unknown SKU and HTTPException(409) when the
    engine reports a BOM cycle.
    """
    order_lines = [(_item_for_sku(session, ln.sku).id, ln.qty) for ln in lines]

    bom_of = make_bom_of(session)
    stock = make_stock(session)
    moq = make_moq(session)

    try:
        gross: dict = {}
        for item_id, qty in order_lines:
            for mat, v in explode(item_id, qty, bom_of).items():
                gross[mat] = gross.get(mat, 0.0) + v
        net = net_requirements(gross, stock)
        suggested_qty = round_to_moq(net, moq)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    all_ids = set(gross) | set(suggested_qty)
    items_by_id = _items_by_id(session, all_ids)

    suggested = []
    for item_id, qty in suggested_qty.items():
        item = items_by_id.get(item_id)
        on_hand, allocated, on_order = stock(item_id)
        suggested.append({
            "sku": item.sku if item else None,
            "name": item.name if item else None,
            "qty": qty,
            "on_hand": on_hand,
            "available": on_hand - allocated + on_order,
            "moq": moq(item_id),
            "vendor": chosen_vendor_name(session, item_id),
        })
    suggested.sort(key=lambda r: (r["sku"] or ""))

    return {
        "gross": _qty_list(items_by_id, gross),
        "net": _qty_list(items_by_id, net),
        "suggested": suggested,
    }


# --------------------------------------------------------------------------- #
# Suggested requisition (mutating)
# --------------------------------------------------------------------------- #
def _require_suggest(user: CurrentUser) -> None:
    if user.role_code not in SUGGEST_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires role: {', '.join(sorted(SUGGEST_ROLES))}",
        )


def suggest_requisition(
    session: Session, user: CurrentUser, lines: list[ExplodeLine],
    cost_center: Optional[str],
) -> dict:
    """Explode+net a demand signal and, for the shortages, create ONE DRAFT
    requisition with source='demand' (one line per shortage material, quantity =
    the MOQ-rounded suggested buy qty). Records an OrderEvent (entity_kind
    REQUISITION) for the audit trail. Returns the created requisition detail.

    No shortages -> create nothing; return {created: False, message: "no shortages"}.

    Reuses the Phase 2 requisition number scheme + serialiser so the requisition is
    indistinguishable from a manually-raised one downstream (approval lifecycle).
    """
    order_lines = [(_item_for_sku(session, ln.sku).id, ln.qty) for ln in lines]

    bom_of = make_bom_of(session)
    stock = make_stock(session)
    moq = make_moq(session)

    try:
        suggested_qty = explode_and_net(order_lines, bom_of, stock, moq)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    if not suggested_qty:
        return {"created": False, "message": "no shortages"}

    # Stable line order (by sku) so the requisition reads deterministically.
    items_by_id = _items_by_id(session, suggested_qty.keys())
    ordered = sorted(
        suggested_qty.items(),
        key=lambda kv: (items_by_id[kv[0]].sku if kv[0] in items_by_id else ""),
    )

    # Reuse the Phase 2 number scheme; retry on the (rare) collision like the
    # manual create path does.
    last_error: Optional[IntegrityError] = None
    for _ in range(5):
        req = Requisition(
            number=req_service._gen_number(),
            requester=user.email,
            status="DRAFT",
            source="demand",
            cost_center=cost_center,
        )
        session.add(req)
        for item_id, qty in ordered:
            session.add(RequisitionLine(
                requisition_id=req.id, item_id=item_id, quantity=qty,
            ))
        session.add(OrderEvent(
            entity_kind=REQ_ENTITY_KIND,
            entity_id=req.id,
            from_status=None,
            to_status="DRAFT",
            event_type="CREATED",
            actor=user.email,
            detail_json=req_service.json.dumps({
                "source": "demand",
                "cost_center": cost_center,
                "line_count": len(ordered),
                "demand": [
                    {"sku": items_by_id[i].sku if i in items_by_id else None,
                     "qty": q}
                    for i, q in ordered
                ],
            }),
        ))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            last_error = exc
            continue
        session.refresh(req)
        return req_service._detail(session, req)

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not allocate a unique requisition number",
    ) from last_error


# --------------------------------------------------------------------------- #
# BOM CRUD — the board-grade spec (SOP step 2's master data)
# --------------------------------------------------------------------------- #
# The app owns the top "kit" level of cross-system BOMs (CLAUDE.md §2): the
# carton -> KG-per-grade spec the forecast explosion runs through. Only
# APP-owned bills are writable here; bills mirrored from Kiwiplan/Accura are
# production truth and stay read-only. Upserts are VERSIONED: the previous
# ACTIVE header is marked OBSOLETE (never deleted), so an audit can always
# reconstruct what spec a past planning run used.

class BomLineIn(BaseModel):
    sku: str
    # qty per 1 unit of the parent (KG of grade per carton for paper specs).
    qty_per: float = Field(gt=0, allow_inf_nan=False)
    # trim/corr-out factors fold in here: 0.04 == 4% over the net weight.
    scrap_pct: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)


class BomIn(BaseModel):
    yield_qty: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    lines: list[BomLineIn] = Field(min_length=1, max_length=100)
    # Optional optimistic-concurrency token: the ACTIVE version the editor loaded.
    # When present it must still match, else another officer has edited since and
    # we 409 instead of clobbering their spec. Omit (None) to force the write.
    base_version: Optional[int] = None


BOM_ENTITY_KIND = "BOM"


def _require_app_owned(header: Optional[BomHeader], action: str) -> None:
    if header is not None and header.owner != BomOwner.APP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot {action}: this bill is mirrored read-only from "
                f"{header.owner.value} (production owns material BOMs)"
            ),
        )


def upsert_bom(session: Session, user: CurrentUser, item: Item,
               body: BomIn) -> dict:
    """Replace the item's APP-owned bill with a new ACTIVE version.

    Validates every component exists and differs from the parent, then proves
    the NEW structure explodes without a cycle before committing (overlay the
    candidate lines on the current bom_of). The previous ACTIVE header becomes
    OBSOLETE in the same transaction; an OrderEvent records the change."""
    current = _active_header(session, item.id)
    _require_app_owned(current, "edit")

    # Optimistic concurrency: reject a save based on a spec that has since moved.
    if body.base_version is not None:
        current_version = current.version if current else 0
        if body.base_version != current_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Spec changed since you opened it (your v{body.base_version} "
                    f"vs current v{current_version}); reload and re-apply."
                ),
            )

    # Resolve + validate components against the catalog.
    lines: list[dict] = []
    seen: set = set()
    for ln in body.lines:
        component = _item_for_sku(session, ln.sku)
        if component.id == item.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="A bill cannot contain its own parent")
        if component.id in seen:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Duplicate component: {ln.sku}")
        seen.add(component.id)
        lines.append({"component_id": component.id, "sku": component.sku,
                      "qty_per": ln.qty_per, "scrap_pct": ln.scrap_pct})

    # Cycle check on the CANDIDATE structure: overlay the new lines for this
    # parent onto the existing bom_of and explode one unit.
    base_bom_of = make_bom_of(session)
    candidate_nodes = [
        BomNode(component=ln["component_id"], qty_per=ln["qty_per"],
                scrap_pct=ln["scrap_pct"])
        for ln in lines
    ]

    def _candidate_bom_of(item_id: str):
        if item_id == item.id:
            return (body.yield_qty, candidate_nodes)
        return base_bom_of(item_id)

    try:
        explode(item.id, 1.0, _candidate_bom_of)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    version = 1
    if current is not None:
        current.status = "OBSOLETE"
        session.add(current)
        version = current.version + 1
    header = BomHeader(
        parent_item_id=item.id,
        version=version,
        status="ACTIVE",
        owner=BomOwner.APP,
        yield_qty=body.yield_qty,
    )
    session.add(header)
    session.flush()
    for i, ln in enumerate(lines, start=1):
        component = session.get(Item, ln["component_id"])
        session.add(BomLine(
            bom_header_id=header.id, line_no=i, component_id=ln["component_id"],
            qty_per=ln["qty_per"], uom=component.uom if component else "EA",
            scrap_pct=ln["scrap_pct"],
        ))
    session.add(OrderEvent(
        entity_kind=BOM_ENTITY_KIND, entity_id=item.id,
        from_status=f"v{current.version}" if current else None,
        to_status=f"v{version}",
        event_type="BOM_UPDATED", actor=user.email,
        detail_json=req_service.json.dumps({
            "sku": item.sku, "version": version, "yield_qty": body.yield_qty,
            "lines": [{"sku": ln["sku"], "qty_per": ln["qty_per"],
                       "scrap_pct": ln["scrap_pct"]} for ln in lines],
        }),
    ))
    session.commit()
    return _bom_tree(session, item)


@router.put("/items/{sku}/bom")
def put_item_bom(
    sku: str,
    body: BomIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_suggest(user)          # same OFFICER/ADMIN gate as the other mutators
    item = _item_for_sku(session, sku)
    return upsert_bom(session, user, item, body)


@router.delete("/items/{sku}/bom", status_code=status.HTTP_204_NO_CONTENT)
def delete_item_bom(
    sku: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """Retire the APP-owned bill (mark OBSOLETE — versions are never deleted,
    the audit trail must be able to reconstruct past planning runs)."""
    _require_suggest(user)
    item = _item_for_sku(session, sku)
    header = _active_header(session, item.id)
    if header is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"{sku} has no active bill")
    _require_app_owned(header, "delete")
    header.status = "OBSOLETE"
    session.add(header)
    session.add(OrderEvent(
        entity_kind=BOM_ENTITY_KIND, entity_id=item.id,
        from_status=f"v{header.version}", to_status=None,
        event_type="BOM_RETIRED", actor=user.email,
        detail_json=req_service.json.dumps({"sku": item.sku,
                                            "version": header.version}),
    ))
    session.commit()
    return None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/items/{sku}/bom")
def get_item_bom(
    sku: str,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    item = _item_for_sku(session, sku)
    try:
        return _bom_tree(session, item)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/bom/explode")
def explode_endpoint(
    body: ExplodeIn,
    session: Session = Depends(get_session),
    _: CurrentUser = Depends(get_current_user),
):
    if not body.lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="At least one line is required"
        )
    return explode_preview(session, body.lines)


@router.post("/bom/suggest-requisition")
def suggest_requisition_endpoint(
    body: SuggestIn,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    _require_suggest(user)
    if not body.lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="At least one line is required"
        )
    return suggest_requisition(session, user, body.lines, body.cost_center)
