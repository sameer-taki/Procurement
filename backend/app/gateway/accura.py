"""Accura adapter — read label-stock + label material requirements via ODBC;
create label jobs via web2print / import. Read-only for procurement.

OPEN QUESTION (CLAUDE.md §7): Accura's open-API support is thin — confirm the
inbound job-creation interface and the ODBC read shape for stock/requirements
with Data Design Services. Until then get_stock serves demo data when
ACCURA_DSN is unset.
"""
from typing import Optional

from ..config import settings
from . import fakes


class AccuraAdapter:
    def __init__(self, dsn=None):
        self.dsn = dsn if dsn is not None else settings.accura_dsn

    @property
    def use_fakes(self) -> bool:
        return settings.fakes_for(settings.accura_enabled)

    def get_stock(self, item_ref: Optional[str]) -> list[dict]:
        """Label-stock rows for a material, one per location:
        [{location, on_hand, allocated, on_order}]. Empty list => unknown/none.

        Live mode runs settings.accura_stock_sql against ACCURA_DSN. Confirm the
        exact ODBC table/columns with Data Design Services (CLAUDE.md §7)."""
        if self.use_fakes:
            return fakes.accura_stock(item_ref)
        from ._odbc import read_stock
        return read_stock(self.dsn, settings.accura_stock_sql, item_ref)

    def get_requirements(self, job: str) -> list[dict]:
        raise NotImplementedError  # Phase 4

    def create_label_job(self, job: dict) -> str:
        raise NotImplementedError  # Phase 4
