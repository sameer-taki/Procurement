"""Business Central adapter — OData v4 over NTLM. On-prem (172.16.1.10),
reachable from the Docker host. BC is the system of record for price/SKU,
customer + vendor masters, posted POs, and invoices.

Live mode (set BC_BASE_URL + BC_USERNAME + BC_PASSWORD) reads the item master from
the OData entity in settings.bc_items_entity. Standard BC field names are assumed
(No / Description / Base_Unit_of_Measure / Unit_Price); confirm these + the entity
name + auth (NavUserPassword=basic vs NTLM) for your BC — see INTEGRATIONS.md and
CLAUDE.md §7. Falls back to demo data when unconfigured.
"""
from typing import Optional

from ..config import settings
from . import fakes

# Standard BC OData V4 item fields. If your BC exposes different names, this is the
# one place to change them.
F_NO = "No"
F_NAME = "Description"
F_UOM = "Base_Unit_of_Measure"
F_PRICE = "Unit_Price"


class BCAdapter:
    def __init__(self, base_url=None, company=None, user=None, password=None):
        self.base_url = base_url if base_url is not None else settings.bc_base_url
        self.company = company if company is not None else settings.bc_company
        self.user = user if user is not None else settings.bc_username
        self.password = password if password is not None else settings.bc_password

    @property
    def use_fakes(self) -> bool:
        return settings.fakes_for(settings.bc_enabled)

    # --- live transport (imported lazily; only used when configured) ---
    def _auth(self):
        if settings.bc_auth.lower() == "ntlm":
            from requests_ntlm import HttpNtlmAuth
            return HttpNtlmAuth(self.user, self.password)
        return (self.user, self.password)

    def _company_url(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/Company('{self.company}')" if self.company else base

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        import requests
        p = {"$format": "json"}
        if params:
            p.update(params)
        r = requests.get(url, auth=self._auth(), params=p,
                         verify=settings.bc_verify_tls, timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _map_item(x: dict) -> dict:
        no = x.get(F_NO)
        return {
            "sku": no,
            "name": x.get(F_NAME) or no,
            "item_type": "MATERIAL",      # TODO: derive from BC item category
            "uom": x.get(F_UOM) or "EA",
            "bc_item_no": no,
            "is_purchased": True,
            "is_made": False,
            "reorder_point": None,        # TODO: map Reorder_Point if used
            "lead_time_days": None,
            "kiwiplan_ref": None,         # TODO: cross-system crosswalk
            "accura_ref": None,
            "sales_price": x.get(F_PRICE),
        }

    # READS
    def list_items(self) -> list[dict]:
        """Item master (incl. price). Follows OData @odata.nextLink pagination."""
        if self.use_fakes:
            return fakes.list_items()
        url = f"{self._company_url()}/{settings.bc_items_entity}"
        out: list[dict] = []
        while url:
            data = self._get(url)
            out.extend(self._map_item(x) for x in data.get("value", []))
            url = data.get("@odata.nextLink")
        return out

    def get_item_price(self, sku: str) -> Optional[float]:
        """Unit price for one SKU (BC item No). Demo data until BC is wired."""
        if self.use_fakes:
            return fakes.item_price(sku)
        url = f"{self._company_url()}/{settings.bc_items_entity}('{sku}')"
        try:
            return self._get(url, {"$select": F_PRICE}).get(F_PRICE)
        except Exception:
            return None

    def get_vendor(self, vendor_no: str) -> Optional[dict]:
        raise NotImplementedError  # Phase 3

    # WRITES
    def create_purchase_order(self, po: dict) -> str:
        """Post a PO to BC; return the BC PO number."""
        raise NotImplementedError  # Phase 3

    def post_sales_invoice(self, order: dict) -> str:
        raise NotImplementedError  # Phase 5
