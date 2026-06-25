"""Accura adapter — read label-stock + label material requirements via ODBC;
create label jobs via web2print / import. Read-only for procurement.
NOTE: Accura's open-API support is thin — confirm the inbound job-creation
interface with Data Design Services before automating job creation."""
from ..config import settings


class AccuraAdapter:
    def __init__(self, dsn=None):
        self.dsn = dsn or settings.accura_dsn

    def get_stock(self, item_ref: str) -> dict | None:
        raise NotImplementedError

    def get_requirements(self, job: str) -> list[dict]:
        raise NotImplementedError

    def create_label_job(self, job: dict) -> str:
        raise NotImplementedError
