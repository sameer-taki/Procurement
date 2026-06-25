"""BOM explosion + netting — the engine behind the procurement trigger.

Pure functions over plain callables so they are unit-testable without a DB.
Wire them to the schema in a thin service layer (Claude Code, Phase 4):
  bom_of(item_id)  -> (yield_qty, [BomNode]) | None   # None => purchased leaf
  stock(item_id)   -> (on_hand, allocated, on_order)
  moq(item_id)     -> float                            # 0/None => no rounding
"""
from dataclasses import dataclass
from math import ceil
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class BomNode:
    component: str
    qty_per: float
    scrap_pct: float = 0.0


Bom = Tuple[float, List[BomNode]]            # (yield_qty, lines)
BomOf = Callable[[str], Optional[Bom]]


def explode(item: str, qty: float, bom_of: BomOf, _seen=None) -> Dict[str, float]:
    """Explode an item+qty down to leaf materials, summing gross requirement."""
    _seen = _seen or frozenset()
    if item in _seen:
        raise ValueError(f"BOM cycle detected at {item}")
    bom = bom_of(item)
    if not bom:                              # purchased leaf
        return {item: qty}
    yield_qty, lines = bom
    seen = _seen | {item}
    out: Dict[str, float] = {}
    for ln in lines:
        q = qty * ln.qty_per / (yield_qty or 1.0)
        q *= (1.0 + ln.scrap_pct)
        for mat, v in explode(ln.component, q, bom_of, seen).items():
            out[mat] = out.get(mat, 0.0) + v
    return out


def net_requirements(gross: Dict[str, float],
                     stock: Callable[[str], Tuple[float, float, float]],
                     eps: float = 1e-9) -> Dict[str, float]:
    """net = gross - (on_hand - allocated + on_order); keep only shortages."""
    res: Dict[str, float] = {}
    for mat, g in gross.items():
        on_hand, allocated, on_order = stock(mat)
        available = on_hand - allocated + on_order
        net = g - available
        if net > eps:
            res[mat] = net
    return res


def round_to_moq(net: Dict[str, float],
                 moq: Callable[[str], Optional[float]]) -> Dict[str, float]:
    """Round each shortage up to MOQ / pack size."""
    out: Dict[str, float] = {}
    for mat, n in net.items():
        m = moq(mat) or 0
        out[mat] = (ceil(n / m) * m) if m else n
    return out


def explode_and_net(order_lines: List[Tuple[str, float]],
                    bom_of: BomOf,
                    stock: Callable[[str], Tuple[float, float, float]],
                    moq: Callable[[str], Optional[float]]) -> Dict[str, float]:
    """Full pipeline for a set of (item_id, qty) order lines -> suggested buy qty."""
    gross: Dict[str, float] = {}
    for item, qty in order_lines:
        for mat, v in explode(item, qty, bom_of).items():
            gross[mat] = gross.get(mat, 0.0) + v
    return round_to_moq(net_requirements(gross, stock), moq)
