"""Paper-planning engine — the 3-month-cover reorder maths from the GML
procurement SOP (Flow of Procurement Procedure — Paper Inventory).

Pure functions over plain values so they are unit-testable without a DB; a thin
service layer (app/domain/planning.py) wires them to the canonical tables. The
decision rule (SOP §8):

    Order Qty (KG) = (COVER_MONTHS x monthly usage) + outstanding demand
                     - on-hand stock - in-transit orders

* Forecasted items: monthly usage = the 3-month customer forecast / 3
  (cartons exploded to KG by grade/deckle through the BOMs upstream).
* Non-forecast items: monthly usage = the average of prior months' movement
  (imported from BC).
* Rounding: convert KG to whole 25-tonne blocks (1 block = 1 x 40ft FCL),
  always UP; grades/deckles are combined per vendor to fill whole containers.
"""
from dataclasses import dataclass, field
from datetime import date
from math import ceil
from typing import Dict, List, Optional

COVER_MONTHS = 3
KG_PER_FCL = 25_000.0          # 25 tonnes = 1 x 40ft FCL (SOP §3/§8)


# --------------------------------------------------------------------------- #
# Period helpers ("YYYY-MM" calendar months) — shared by the planning service
# and the demo data so windows are always computed one way.
# --------------------------------------------------------------------------- #
def forward_periods(n: int, today: Optional[date] = None) -> List[str]:
    """The current month + the next n-1, oldest first."""
    today = today or date.today()
    year, month = today.year, today.month
    out: List[str] = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def trailing_periods(n: int, today: Optional[date] = None) -> List[str]:
    """The n calendar months before the current one, oldest first."""
    today = today or date.today()
    year, month = today.year, today.month
    out: List[str] = []
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f"{year:04d}-{month:02d}")
    return list(reversed(out))


def trailing_average(monthly_qty: List[float]) -> float:
    """Average movement over the prior months that actually have a figure.

    `monthly_qty` is the per-month consumption for the trailing window (most
    recent months of usage_history). Months with zero recorded movement still
    count — a slow month is real data — but an empty window means 'no basis'
    and returns 0 so the caller can flag the item instead of dividing by air.
    """
    if not monthly_qty:
        return 0.0
    return sum(monthly_qty) / len(monthly_qty)


def usage_basis(forecast_kg: float, forecast_periods: int,
                history_avg: float) -> tuple:
    """Choose the monthly-usage figure and its basis for one grade/deckle.

    The SOP prefers the customer forecast, but two partial-coverage traps make a
    naive 'forecast wins outright' rule under-plan long-lead paper:
      * a partially ENTERED window (sales have typed 1 of the 3 months) must not
        treat the missing months as zero — divide by the months actually covered;
      * a partially COVERED grade (only some customers forecast; the rest of the
        movement shows up only in history) must not discard a higher trailing
        average — never plan below actual movement.
    So: forecast average = forecast KG / covered months, and the planning figure
    is max(forecast average, history average). Basis names the winning source;
    an intentional ramp-down (real forecast below history) shows as HISTORY with
    both figures surfaced, for the planner to override by cleaning usage history.

    Returns (monthly_usage, basis) with basis FORECAST | HISTORY | NONE.
    """
    forecast_avg = (forecast_kg / forecast_periods) if forecast_periods else 0.0
    monthly = max(forecast_avg, history_avg)
    if monthly <= 0:
        return 0.0, "NONE"
    basis = "FORECAST" if forecast_periods and forecast_avg >= history_avg else "HISTORY"
    return monthly, basis


def months_of_stock(on_hand: float, in_transit: float,
                    monthly_usage: float) -> Optional[float]:
    """The SOP coverage metric: (on-hand + in-transit) / monthly usage.

    None when there is no usage basis (metric undefined, not infinite) so the
    Order Page can show '—' rather than a misleading number.
    """
    if monthly_usage <= 0:
        return None
    return (on_hand + in_transit) / monthly_usage


def order_quantity(monthly_usage: float, outstanding_demand: float,
                   on_hand: float, in_transit: float,
                   cover_months: int = COVER_MONTHS) -> float:
    """SOP §8 raw requirement in KG (before container rounding); never negative."""
    need = (cover_months * monthly_usage) + outstanding_demand - on_hand - in_transit
    return max(0.0, need)


def round_up_to_block(kg: float, block_kg: float = KG_PER_FCL) -> float:
    """Round a KG requirement UP to the nearest whole block (25t by default).
    A zero/None block means no rounding (non-container-bound materials)."""
    if kg <= 0:
        return 0.0
    if not block_kg:
        return kg
    return ceil(kg / block_kg - 1e-9) * block_kg


@dataclass
class PlanLine:
    """One grade/deckle requirement inside a container plan."""
    item_id: str
    requirement_kg: float          # raw SOP §8 requirement
    order_kg: float = 0.0          # allocated after container consolidation


@dataclass
class ContainerPlan:
    """A per-vendor consolidation: whole containers covering several lines."""
    vendor_id: Optional[str]
    containers: int
    capacity_kg: float
    total_kg: float                # == containers * block (sum of order_kg)
    lines: List[PlanLine] = field(default_factory=list)


def consolidate_containers(lines: List[PlanLine],
                           block_kg: float = KG_PER_FCL,
                           vendor_id: Optional[str] = None) -> Optional[ContainerPlan]:
    """Combine one vendor's grade/deckle requirements into whole containers.

    SOP §8: order volumes go in multiples of 25 tonnes, consolidating grades /
    deckles to fill each container — i.e. the VENDOR TOTAL rounds up to whole
    FCLs, not each line individually (which would over-order small deckles).
    The slack between the raw total and the container capacity is topped up on
    the largest-requirement line: filling with the highest-running grade is the
    planner's own rule of thumb and keeps every other line at its computed need.

    Lines with no requirement are dropped; no requirement at all -> None.
    """
    live = [ln for ln in lines if ln.requirement_kg > 0]
    if not live:
        return None
    total = sum(ln.requirement_kg for ln in live)
    if block_kg:
        containers = int(ceil(total / block_kg - 1e-9))
        capacity = containers * block_kg
    else:                          # no container discipline for this vendor
        containers = 0
        capacity = total
    for ln in live:
        ln.order_kg = ln.requirement_kg
    slack = capacity - total
    if slack > 0:
        biggest = max(live, key=lambda ln: ln.requirement_kg)
        biggest.order_kg += slack
    return ContainerPlan(
        vendor_id=vendor_id,
        containers=containers,
        capacity_kg=block_kg or 0.0,
        total_kg=capacity,
        lines=sorted(live, key=lambda ln: ln.item_id),
    )


def plan_orders(requirements_by_vendor: Dict[Optional[str], Dict[str, float]],
                block_kg: float = KG_PER_FCL) -> List[ContainerPlan]:
    """Full consolidation pass: {vendor_id: {item_id: requirement_kg}} ->
    one ContainerPlan per vendor that has any requirement."""
    plans: List[ContainerPlan] = []
    for vendor_id, reqs in requirements_by_vendor.items():
        lines = [PlanLine(item_id=i, requirement_kg=kg) for i, kg in reqs.items()]
        plan = consolidate_containers(lines, block_kg=block_kg, vendor_id=vendor_id)
        if plan is not None:
            plans.append(plan)
    return sorted(plans, key=lambda p: (p.vendor_id or ""))
