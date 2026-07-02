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
            # TODO: confirm which BC item-master fields carry the paper grade +
            # deckle (item category / attributes vs a naming convention on No).
            "grade": None,
            "deckle_mm": None,
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

    def get_usage_entries(self) -> list[dict]:
        """Monthly consumption per item — the BC usage export the planning run
        imports (SOP step 3). The usage originates in Kiwiplan (paper consumed by
        Jobs) and is passed to BC; BC's item ledger is the export we read.

        Returns [{sku, period 'YYYY-MM', quantity}] with quantity in the item's
        base UOM (KG for paper). Demo data until BC is wired.

        Live mode reads the item-ledger OData entity (settings.bc_usage_entity)
        filtered to consumption entries and aggregates client-side by item +
        posting month. Standard BC V4 field names are assumed (Item_No /
        Posting_Date / Quantity, consumption negative); confirm the entity name +
        which Entry_Type values represent Kiwiplan-fed usage for this tenant
        (CLAUDE.md §7) before going live.
        """
        if self.use_fakes:
            return fakes.usage_entries()

        url = f"{self._company_url()}/{settings.bc_usage_entity}"
        by_item_month: dict[tuple, float] = {}
        params = {
            # TODO: confirm the Entry_Type filter value(s) for this tenant.
            "$filter": "Entry_Type eq 'Consumption'",
            "$select": "Item_No,Posting_Date,Quantity",
        }
        while url:
            data = self._get(url, params)
            params = None          # nextLink already carries the query
            for x in data.get("value", []):
                sku = x.get("Item_No")
                posted = str(x.get("Posting_Date") or "")
                if not sku or len(posted) < 7:
                    continue
                period = posted[:7]
                # Consumption posts negative quantities in the item ledger.
                qty = abs(float(x.get("Quantity") or 0))
                key = (sku, period)
                by_item_month[key] = by_item_month.get(key, 0.0) + qty
            url = data.get("@odata.nextLink")
        return [
            {"sku": sku, "period": period, "quantity": qty}
            for (sku, period), qty in sorted(by_item_month.items())
        ]

    def get_vendor(self, vendor_no: str) -> Optional[dict]:
        raise NotImplementedError  # Phase 3

    # WRITES
    def create_purchase_order(self, po: dict) -> str:
        """Post a PO to BC and return the BC PO number.

        Demo mode (BC unconfigured) returns a deterministic fake "BCPO-<8 hex>"
        derived from the canonical po_id, so the same PO always maps to the same
        fake BC number (the outbox idempotency guard relies on stable values).

        Live mode POSTs to the BC purchase-order OData entity. Standard BC V4
        field names are assumed (Buy_from_Vendor_No / lines via the
        PurchaseOrderLines navigation); confirm the exact entity + line shape +
        document-no behaviour for your BC (see CLAUDE.md §7) before going live.
        """
        if self.use_fakes:
            import hashlib
            seed = str(po.get("po_id") or po.get("number") or po)
            return "BCPO-" + hashlib.sha1(seed.encode()).hexdigest()[:8].upper()

        # --- live OData POST (standard skeleton) ---
        import requests
        url = f"{self._company_url()}/{settings.bc_po_entity}"
        body = {
            # TODO: confirm BC purchase-order field names + how lines are posted
            # (inline navigation vs a separate line entity) for this tenant.
            "Buy_from_Vendor_No": po.get("vendor_bc_no") or po.get("vendor_no"),
            "External_Document_No": po.get("number"),
        }
        r = requests.post(
            url, auth=self._auth(), json=body,
            params={"$format": "json"},
            verify=settings.bc_verify_tls, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # TODO: confirm the field the posted document number is returned in.
        return data.get("No") or data.get(F_NO)

    def post_purchase_order_lines(self, bc_po_no: str, lines: list[dict]) -> None:
        raise NotImplementedError  # Phase 3 live-mode line posting (TODO: confirm shape)

    def post_receipt(self, receipt: dict) -> str:
        """Post a goods receipt (GRN) to BC and return the BC GRN/receipt number.

        Demo mode (BC unconfigured) returns a deterministic fake "BCGRN-<8 hex>"
        derived from the canonical grn_no, so the same GRN always maps to the same
        fake BC number (the outbox idempotency guard relies on stable values).

        Live mode POSTs to the BC purchase-receipt OData entity. The standard
        flow is to post against the open purchase order's lines; confirm the exact
        receipt entity + how received quantities are posted (Qty. to Receive on the
        PO line vs a posted-receipt header) for this BC (CLAUDE.md §7) before going
        live. BC then owns the posted receipt and the 3-way match.
        """
        if self.use_fakes:
            import hashlib
            seed = str(receipt.get("grn_no") or receipt.get("po_id") or receipt)
            return "BCGRN-" + hashlib.sha1(seed.encode()).hexdigest()[:8].upper()

        # --- live OData POST (standard skeleton) ---
        import requests
        # TODO: confirm the BC purchase-receipt entity + payload shape for this
        # tenant (post Qty. to Receive on the PO then Post, vs a receipt entity).
        url = f"{self._company_url()}/{settings.bc_receipt_entity}"
        body = {
            "Document_No": receipt.get("bc_po_no") or receipt.get("po_number"),
            "External_Document_No": receipt.get("grn_no"),
        }
        r = requests.post(
            url, auth=self._auth(), json=body,
            params={"$format": "json"},
            verify=settings.bc_verify_tls, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # TODO: confirm the field the posted receipt number is returned in.
        return data.get("No") or data.get(F_NO)

    def get_match_status(self, po: dict) -> str:
        """Return BC's reported 3-way match state for a PO (PO·GRN·invoice).

        BC owns the match (CLAUDE.md §2): this app never fabricates money, it only
        reflects what BC reports. Demo mode has no separate invoice, so it returns
        'MATCHED' once goods are received (the receipt is the demo trigger).

        Live mode would poll BC for the document's match/invoice status. Returns one
        of BC's states (e.g. 'MATCHED', 'PENDING_INVOICE', 'UNMATCHED').
        """
        if self.use_fakes:
            return "MATCHED"

        # --- live: poll BC for the posted-invoice / match status (TODO) ---
        # TODO: confirm how BC exposes the 3-way-match outcome for a purchase
        # document (posted-invoice link, a status field, or a dedicated query) and
        # map it to one of our match states. Default to PENDING until wired.
        return "PENDING_INVOICE"

    def post_sales_invoice(self, order: dict) -> str:
        raise NotImplementedError  # Phase 5
