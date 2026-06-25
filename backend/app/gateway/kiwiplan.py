"""Kiwiplan adapter — read stock + resolved material requirements via KDW/SQL;
inject production orders via KMC / Transmission Links. Read-only for procurement.

OPEN QUESTION (CLAUDE.md §7): confirm with Advantive which inbound channel (KMC /
Transmission Links) and which requirement/stock views your licence exposes via
KDW/SQL before relying on inject/requirements. Until then get_stock serves demo
data when KIWIPLAN_DSN is unset.
"""
from typing import Optional

from ..config import settings
from . import fakes


class KiwiplanAdapter:
    def __init__(self, dsn=None):
        self.dsn = dsn if dsn is not None else settings.kiwiplan_dsn

    @property
    def use_fakes(self) -> bool:
        return settings.fakes_for(settings.kiwiplan_enabled)

    def get_stock(self, item_ref: Optional[str]) -> list[dict]:
        """Stock rows for a roll-stock/plant material, one per location:
        [{location, on_hand, allocated, on_order}]. Empty list => unknown/none.

        Live mode runs settings.kiwiplan_stock_sql against KIWIPLAN_DSN. Confirm
        the exact KDW view/columns with Advantive (CLAUDE.md §7) and set that SQL."""
        if self.use_fakes:
            return fakes.kiwiplan_stock(item_ref)
        from ._odbc import read_stock
        return read_stock(self.dsn, settings.kiwiplan_stock_sql, item_ref)

    def get_requirements(self, production_order: str) -> list[dict]:
        raise NotImplementedError  # Phase 4

    def inject_production_order(self, order: dict) -> str:
        raise NotImplementedError  # Phase 4
