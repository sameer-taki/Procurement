"""Kiwiplan adapter — read stock + resolved material requirements via KDW/SQL;
inject production orders via KMC / Transmission Links. Read-only for procurement.
NOTE: confirm with Advantive which inbound channel + which requirement views your
licence actually exposes before relying on inject/requirements."""
from ..config import settings


class KiwiplanAdapter:
    def __init__(self, dsn=None):
        self.dsn = dsn or settings.kiwiplan_dsn

    def get_stock(self, item_ref: str) -> dict | None:
        """on_hand / allocated / on_order for a roll-stock material. TODO (KDW/SQL)."""
        raise NotImplementedError

    def get_requirements(self, production_order: str) -> list[dict]:
        """Resolved material requirements for a production order. TODO."""
        raise NotImplementedError

    def inject_production_order(self, order: dict) -> str:
        """Create a production order via KMC; return Kiwiplan ref. TODO."""
        raise NotImplementedError
