"""Business Central adapter — OData v4 over NTLM. On-prem (172.16.1.10),
reachable from the Docker host. BC is the system of record for price/SKU,
customer + vendor masters, posted POs, and invoices."""
from ..config import settings


class BCAdapter:
    def __init__(self, base_url=None, company=None, user=None, password=None):
        self.base_url = base_url or settings.bc_base_url
        self.company = company or settings.bc_company
        self.user = user or settings.bc_username
        self.password = password or settings.bc_password

    # READS
    def get_item_price(self, sku: str) -> float | None:
        """Selling price per SKU from a BC price list. TODO: OData query + NTLM."""
        raise NotImplementedError

    def get_vendor(self, vendor_no: str) -> dict | None:
        raise NotImplementedError

    def list_items(self) -> list[dict]:
        raise NotImplementedError

    # WRITES
    def create_purchase_order(self, po: dict) -> str:
        """Post a PO to BC; return the BC PO number. TODO."""
        raise NotImplementedError

    def post_sales_invoice(self, order: dict) -> str:
        raise NotImplementedError
