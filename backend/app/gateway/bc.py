"""Business Central adapter — OData v4 over NTLM. On-prem (172.16.1.10),
reachable from the Docker host. BC is the system of record for price/SKU,
customer + vendor masters, posted POs, and invoices.

Live mode (set BC_BASE_URL + BC_USERNAME + BC_PASSWORD) speaks standard BC V4
page-based OData; every entity set, field name and mapping convention it assumes
is a setting (config.py `bc_*` / `crosswalk_mode`), so pointing it at a tenant
whose names differ is an env-var change, not a code change — see INTEGRATIONS.md.
Falls back to demo data when unconfigured.
"""
import re
from typing import Optional

from ..config import settings
from . import fakes

# Standard BC OData V4 field names shared across entities. If your BC exposes
# different names, this is the one place to change them.
F_NO = "No"
F_NAME = "Description"
F_UOM = "Base_Unit_of_Measure"
F_PRICE = "Unit_Price"
F_INVENTORY = "Inventory"
F_VENDOR_NAME = "Name"
F_VENDOR_EMAIL = "E_Mail"
# Purchase price list (settings.bc_purchase_prices_entity) fields.
F_PP_ITEM = "Item_No"
F_PP_VENDOR = "Vendor_No"
F_PP_COST = "Direct_Unit_Cost"
F_PP_MOQ = "Minimum_Quantity"


def _parse_dateformula_days(value) -> Optional[int]:
    """BC lead time is a dateformula string ('45D', '<2W>', '1M'); normalise to
    days. Unknown/blank shapes -> None (never guess a lead time)."""
    m = re.search(r"(\d+)\s*([DWM])", str(value or "").upper())
    if not m:
        return None
    return int(m.group(1)) * {"D": 1, "W": 7, "M": 30}[m.group(2)]


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

    def _send(self, method: str, url: str, body: Optional[dict] = None,
              if_match: Optional[str] = None) -> dict:
        """POST/PATCH against BC. If-Match is required by BC for updates; '*'
        (last-writer-wins) is fine for the idempotent writes this adapter does."""
        import requests
        headers = {"Accept": "application/json"}
        if if_match:
            headers["If-Match"] = if_match
        r = requests.request(
            method, url, auth=self._auth(), json=body or {},
            params={"$format": "json"}, headers=headers,
            verify=settings.bc_verify_tls, timeout=30,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    @staticmethod
    def _paper_attrs(x: dict, no: str) -> tuple:
        """(grade, deckle_mm) for one item-master row (SOP §3: stock is tracked
        by grade AND deckle). Explicit OData fields win when configured; else the
        item No is parsed against the grade-deckle SKU convention (CWT140-1400).
        Anything that matches neither is simply not roll stock -> (None, None)."""
        if settings.bc_grade_field or settings.bc_deckle_field:
            grade = (x.get(settings.bc_grade_field) or None) if settings.bc_grade_field else None
            deckle = None
            if settings.bc_deckle_field:
                try:
                    raw = x.get(settings.bc_deckle_field)
                    deckle = int(float(raw)) if raw not in (None, "", 0, "0") else None
                except (TypeError, ValueError):
                    deckle = None
            return grade, deckle
        m = re.match(settings.bc_paper_sku_regex, no or "") if settings.bc_paper_sku_regex else None
        if m:
            groups = m.groups()
            grade = groups[0] if groups else None
            # The deckle group is OPTIONAL: GML's BC keys one item per GRADE
            # (item No 'WTL175', no deckle suffix — confirmed on BC140), so a
            # grade-only regex must parse cleanly with deckle left None.
            deckle = None
            if len(groups) > 1 and groups[1]:
                try:
                    deckle = int(groups[1])
                except (TypeError, ValueError):
                    deckle = None
            return grade, deckle
        return None, None

    @staticmethod
    def _crosswalk(x: dict, no: str) -> tuple:
        """(kiwiplan_ref, accura_ref) per settings.crosswalk_mode. 'sku' assumes
        the BC item No IS the material code in both systems (a stock read for a
        code a system doesn't carry just returns no rows)."""
        mode = settings.crosswalk_mode.lower()
        if mode == "sku":
            return no, no
        if mode == "fields":
            kiwi = x.get(settings.bc_kiwiplan_ref_field) if settings.bc_kiwiplan_ref_field else None
            accura = x.get(settings.bc_accura_ref_field) if settings.bc_accura_ref_field else None
            return kiwi or None, accura or None
        return None, None            # 'none' (or unknown): map later

    @classmethod
    def _map_item(cls, x: dict) -> dict:
        no = x.get(F_NO)
        grade, deckle = cls._paper_attrs(x, no)
        kiwiplan_ref, accura_ref = cls._crosswalk(x, no)
        reorder = None
        if settings.bc_reorder_point_field:
            try:
                raw = float(x.get(settings.bc_reorder_point_field) or 0)
                reorder = raw if raw > 0 else None      # BC's 0 means 'not set'
            except (TypeError, ValueError):
                reorder = None
        lead = _parse_dateformula_days(
            x.get(settings.bc_lead_time_field)) if settings.bc_lead_time_field else None
        made = bool(
            settings.bc_replenishment_field
            and str(x.get(settings.bc_replenishment_field) or "").strip() == "Prod. Order"
        )
        return {
            "sku": no,
            "name": x.get(F_NAME) or no,
            "item_type": "FINISHED" if made else "MATERIAL",
            "uom": x.get(F_UOM) or "EA",
            "bc_item_no": no,
            "is_purchased": not made,
            "is_made": made,
            "reorder_point": reorder,
            "lead_time_days": lead,
            "kiwiplan_ref": kiwiplan_ref,
            "accura_ref": accura_ref,
            "sales_price": x.get(F_PRICE),
            "grade": grade,
            "deckle_mm": deckle,
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

    def get_usage_entries(self, months: int = 6) -> list[dict]:
        """Monthly consumption per item — the BC usage export the planning run
        imports (SOP step 3). The usage originates in Kiwiplan (paper consumed by
        Jobs) and is passed to BC; BC's item ledger is the export we read.

        Returns [{sku, period 'YYYY-MM', quantity}] with quantity in the item's
        base UOM (KG for paper), covering the trailing `months` calendar months
        (the planning window plus headroom — never the whole ledger, which on a
        live BC is years of postings and would stall the import for minutes).
        Demo data until BC is wired.

        Live mode reads the item-ledger OData entity (settings.bc_usage_entity)
        filtered to the usage Entry_Type values (settings.bc_usage_entry_types)
        since the window start and aggregates client-side by item + posting
        month. Quantities are SUMMED SIGNED per month, then flipped: usage posts
        negative and a reversal / correction posts positive, so netting (not
        abs-per-entry) keeps a corrected posting from inflating usage.

        Verified against GML's BC14 (instance BC140): ItemLedgerEntries is
        published as a QUERY-object web service ($filter/$select/$top fine, no
        $orderby — which this never uses), field names are Item_No /
        Posting_Date ('YYYY-MM-DD') / Quantity / Entry_Type, and Kiwiplan job
        consumption posts as 'Negative Adjmt.'.
        """
        if self.use_fakes:
            return fakes.usage_entries()

        from .planning import trailing_periods
        window_start = trailing_periods(months)[0] + "-01"
        entry_types = [
            t.strip() for t in settings.bc_usage_entry_types.split(",") if t.strip()
        ]
        type_filter = " or ".join(f"Entry_Type eq '{t}'" for t in entry_types)
        url = f"{self._company_url()}/{settings.bc_usage_entity}"
        by_item_month: dict[tuple, float] = {}
        params = {
            "$filter": f"({type_filter}) and Posting_Date ge {window_start}",
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
                key = (sku, period)
                by_item_month[key] = by_item_month.get(key, 0.0) + float(x.get("Quantity") or 0)
            url = data.get("@odata.nextLink")
        # Net consumption is the negative of the signed sum; a month whose
        # corrections outweigh its consumption clamps to zero, never negative.
        return [
            {"sku": sku, "period": period, "quantity": max(0.0, -qty)}
            for (sku, period), qty in sorted(by_item_month.items())
        ]

    def get_inventory(self) -> dict:
        """On-hand inventory per item ({sku: quantity in base UOM}) — BC's
        paper-inventory figure, maintained from Kiwiplan's usage postings.

        Used by the SOP §9 reconciliation control: BC's figure is checked against
        the operational roll stock the production systems report, and variances by
        grade/deckle are surfaced for investigation. Demo data until BC is wired.

        Live mode reads the item master with $select=No,Inventory (the standard
        BC V4 flowfield; TODO: confirm the field name for this tenant — a location
        -filtered Item_Ledger balance may be needed if Inventory spans locations
        the app does not track).
        """
        if self.use_fakes:
            return fakes.bc_inventory()
        url = f"{self._company_url()}/{settings.bc_items_entity}"
        params = {"$select": f"{F_NO},{F_INVENTORY}"}
        out: dict = {}
        while url:
            data = self._get(url, params)
            params = None          # nextLink already carries the query
            for x in data.get("value", []):
                no = x.get(F_NO)
                if no:
                    out[no] = float(x.get(F_INVENTORY) or 0)
            url = data.get("@odata.nextLink")
        return out

    def list_vendors(self) -> list[dict]:
        """Vendor master: [{bc_vendor_no, name, email}]. BC owns vendors
        (CLAUDE.md §2); live mode syncs them into the app's read-only mirror."""
        if self.use_fakes:
            return fakes.vendors()
        url = f"{self._company_url()}/{settings.bc_vendors_entity}"
        out: list[dict] = []
        params = {"$select": f"{F_NO},{F_VENDOR_NAME},{F_VENDOR_EMAIL}"}
        while url:
            data = self._get(url, params)
            params = None
            for x in data.get("value", []):
                no = x.get(F_NO)
                if no:
                    out.append({
                        "bc_vendor_no": no,
                        "name": x.get(F_VENDOR_NAME) or no,
                        "email": x.get(F_VENDOR_EMAIL) or None,
                    })
            url = data.get("@odata.nextLink")
        return out

    def get_vendor(self, vendor_no: str) -> Optional[dict]:
        """One vendor by BC No; None when unknown."""
        if self.use_fakes:
            for v in fakes.vendors():
                if v.get("bc_vendor_no") == vendor_no:
                    return dict(v)
            return None
        url = f"{self._company_url()}/{settings.bc_vendors_entity}('{vendor_no}')"
        try:
            x = self._get(url, {"$select": f"{F_NO},{F_VENDOR_NAME},{F_VENDOR_EMAIL}"})
        except Exception:
            return None
        no = x.get(F_NO)
        if not no:
            return None
        return {"bc_vendor_no": no, "name": x.get(F_VENDOR_NAME) or no,
                "email": x.get(F_VENDOR_EMAIL) or None}

    def list_vendor_prices(self) -> list[dict]:
        """Vendor price list: [{sku, vendor_no, price, moq, lead_time_days}] —
        BC's price-per-SKU (CLAUDE.md §2: BC owns price). Live mode reads the
        purchase-price entity (settings.bc_purchase_prices_entity, standard
        fields Item_No / Vendor_No / Direct_Unit_Cost / Minimum_Quantity);
        lead_time_days is not on the price list, so live rows carry None and
        vendor selection falls back to the item's own lead time."""
        if self.use_fakes:
            by_name = {v["name"]: v.get("bc_vendor_no") for v in fakes.vendors()}
            return [{
                "sku": r["sku"],
                "vendor_no": by_name.get(r["vendor"]),
                "price": r["price"],
                "moq": r.get("moq"),
                "lead_time_days": r.get("lead_time_days"),
            } for r in fakes.vendor_prices()]
        url = f"{self._company_url()}/{settings.bc_purchase_prices_entity}"
        out: list[dict] = []
        params = {"$select": f"{F_PP_ITEM},{F_PP_VENDOR},{F_PP_COST},{F_PP_MOQ}"}
        while url:
            data = self._get(url, params)
            params = None
            for x in data.get("value", []):
                sku = x.get(F_PP_ITEM)
                vendor_no = x.get(F_PP_VENDOR)
                if not sku or not vendor_no:
                    continue
                out.append({
                    "sku": sku,
                    "vendor_no": vendor_no,
                    "price": float(x.get(F_PP_COST) or 0),
                    "moq": float(x.get(F_PP_MOQ)) if x.get(F_PP_MOQ) else None,
                    "lead_time_days": None,
                })
            url = data.get("@odata.nextLink")
        return out

    # WRITES
    def _find_po_no(self, number: str) -> Optional[str]:
        """The BC document No of an order previously created for our PO number
        (External_Document_No carries it) — the retry-safety lookup."""
        url = f"{self._company_url()}/{settings.bc_po_entity}"
        data = self._get(url, {
            "$filter": f"External_Document_No eq '{number}'",
            "$select": F_NO, "$top": "1",
        })
        values = data.get("value") or []
        return values[0].get(F_NO) if values else None

    def _existing_line_items(self, bc_po_no: str) -> tuple[set, int]:
        """(item Nos already on the BC order, highest Line_No) — the retry-safety
        read that lets us post only the lines a previous attempt didn't."""
        url = f"{self._company_url()}/{settings.bc_po_lines_entity}"
        data = self._get(url, {
            "$filter": f"Document_No eq '{bc_po_no}'",
            "$select": f"Line_No,{F_NO}",
        })
        items: set = set()
        max_line = 0
        for x in data.get("value") or []:
            if x.get(F_NO):
                items.add(x.get(F_NO))
            max_line = max(max_line, int(x.get("Line_No") or 0))
        return items, max_line

    def post_purchase_order_lines(self, bc_po_no: str, lines: list[dict]) -> int:
        """Post the order's lines, skipping ONLY the items already on the document
        so a retry after a partial post completes the missing lines instead of
        dropping them. Returns how many were written. Standard page fields:
        Document_Type/Document_No/Line_No/Type/No/Quantity/Direct_Unit_Cost;
        Line_No continues in steps of 10000 past the highest existing line."""
        if self.use_fakes:
            return 0
        existing_items, max_line = self._existing_line_items(bc_po_no)
        url = f"{self._company_url()}/{settings.bc_po_lines_entity}"
        written = 0
        next_line = max_line
        for ln in lines:
            item_no = ln.get("bc_item_no") or ln.get("sku")
            if item_no in existing_items:
                continue                    # already posted by a prior attempt
            next_line += 10000
            self._send("post", url, {
                "Document_Type": "Order",
                "Document_No": bc_po_no,
                "Line_No": next_line,
                "Type": "Item",
                "No": item_no,
                "Quantity": ln.get("quantity"),
                "Direct_Unit_Cost": ln.get("unit_price"),
            })
            existing_items.add(item_no)
            written += 1
        return written

    def create_purchase_order(self, po: dict) -> str:
        """Post a PO (header + lines) to BC and return the BC PO number.

        Demo mode (BC unconfigured) returns a deterministic fake "BCPO-<8 hex>"
        derived from the canonical po_id, so the same PO always maps to the same
        fake BC number (the outbox idempotency guard relies on stable values).

        Live mode is retry-safe end to end: our PO number rides in
        External_Document_No, so a retried outbox row first looks the document up
        and reuses it (posting any lines a previous attempt didn't get to) rather
        than creating a duplicate header.
        """
        if self.use_fakes:
            import hashlib
            seed = str(po.get("po_id") or po.get("number") or po)
            return "BCPO-" + hashlib.sha1(seed.encode()).hexdigest()[:8].upper()

        number = po.get("number")
        lines = po.get("lines") or []
        bc_po_no = self._find_po_no(number) if number else None
        if bc_po_no is None:
            data = self._send("post", f"{self._company_url()}/{settings.bc_po_entity}", {
                "Buy_from_Vendor_No": po.get("vendor_bc_no") or po.get("vendor_no"),
                "External_Document_No": number,
            })
            bc_po_no = data.get(F_NO)
            if not bc_po_no:
                raise RuntimeError(
                    f"BC did not return a document No for PO {number} "
                    f"(response keys: {sorted(data)})"
                )
        self.post_purchase_order_lines(bc_po_no, lines)
        return bc_po_no

    def post_receipt(self, receipt: dict) -> str:
        """Post a goods receipt (GRN) to BC and return the BC receipt number.

        Demo mode (BC unconfigured) returns a deterministic fake "BCGRN-<8 hex>"
        derived from the canonical grn_no, so the same GRN always maps to the same
        fake BC number (the outbox idempotency guard relies on stable values).

        Live mode follows the standard BC receive pattern:
          1. read the posted order's lines to map item No -> Line_No;
          2. PATCH Qty_to_Receive on each received line (If-Match: *);
          3. invoke the posting action (settings.bc_receipt_post_action) on the
             order — BC creates the posted receipt and owns it from there;
          4. read back the newest posted receipt for the order as the GRN number.
        Step 4 is best-effort: once step 3 succeeds the receipt EXISTS in BC, so a
        lookup hiccup falls back to a deterministic reference rather than raising
        (raising would retry the whole post and double-receive).

        RESIDUAL RISK (confirm before enabling live receipt writes): if the step-3
        Post action itself times out with no response, the receipt may or may not
        have posted, and a retry re-runs steps 2-3 -> a possible DOUBLE receive.
        Making this leg exactly-once needs a tenant-specific correlation key (e.g.
        stamping the grn_no where the posted receipt is queryable), which must be
        verified against the real BC first. Until then, run receipt posting with a
        read-only BC account or reconcile posted receipts manually (CLAUDE.md §7).
        """
        if self.use_fakes:
            import hashlib
            seed = str(receipt.get("grn_no") or receipt.get("po_id") or receipt)
            return "BCGRN-" + hashlib.sha1(seed.encode()).hexdigest()[:8].upper()

        bc_po_no = receipt.get("bc_po_no")
        if not bc_po_no:
            raise RuntimeError(
                f"GRN {receipt.get('grn_no')}: PO not posted to BC yet "
                "(no bc_po_no) — receipt will retry after the PO posts"
            )

        # 1. Item No -> Line_No on the BC order.
        lines_url = f"{self._company_url()}/{settings.bc_po_lines_entity}"
        data = self._get(lines_url, {
            "$filter": f"Document_No eq '{bc_po_no}'",
            "$select": f"Line_No,{F_NO}",
        })
        line_no_by_item = {x.get(F_NO): x.get("Line_No") for x in data.get("value", [])}

        # 2. Qty_to_Receive per received line (summed per item within the GRN).
        qty_by_item: dict = {}
        for ln in receipt.get("lines") or []:
            item_no = ln.get("bc_item_no") or ln.get("sku")
            if not item_no:
                raise RuntimeError(
                    f"GRN {receipt.get('grn_no')}: received line missing an item No"
                )
            qty_by_item[item_no] = qty_by_item.get(item_no, 0.0) + float(ln.get("quantity") or 0)
        for item_no, qty in qty_by_item.items():
            line_no = line_no_by_item.get(item_no)
            if line_no is None:
                raise RuntimeError(
                    f"GRN {receipt.get('grn_no')}: item {item_no} not on BC order {bc_po_no}"
                )
            self._send(
                "patch",
                f"{lines_url}(Document_Type='Order',Document_No='{bc_po_no}',Line_No={line_no})",
                {"Qty_to_Receive": qty},
                if_match="*",
            )

        # 3. Post the receive.
        self._send(
            "post",
            f"{self._company_url()}/{settings.bc_po_entity}('{bc_po_no}')/{settings.bc_receipt_post_action}",
        )

        # 4. The posted receipt's number (best-effort; see docstring).
        try:
            data = self._get(f"{self._company_url()}/{settings.bc_receipt_entity}", {
                "$filter": f"Order_No eq '{bc_po_no}'",
                "$select": F_NO, "$orderby": f"{F_NO} desc", "$top": "1",
            })
            values = data.get("value") or []
            if values and values[0].get(F_NO):
                return values[0][F_NO]
        except Exception:
            pass
        return f"BC-RCPT-{receipt.get('grn_no')}"

    def get_match_status(self, po: dict) -> str:
        """Return BC's reported 3-way match state for a PO (PO·GRN·invoice).

        BC owns the match (CLAUDE.md §2): this app never fabricates money, it only
        reflects what BC reports. Demo mode has no separate invoice, so it returns
        'MATCHED' once goods are received (the receipt is the demo trigger).

        Live mode checks for a posted purchase invoice referencing the order
        (settings.bc_invoice_entity, standard Order_No field): BC only posts the
        invoice once it reconciles PO·GRN·invoice, so a posted invoice IS the
        matched signal. No invoice yet -> PENDING_INVOICE.
        """
        if self.use_fakes:
            return "MATCHED"
        bc_po_no = po.get("bc_po_no") or po.get("po_number")
        if not bc_po_no:
            return "PENDING_INVOICE"
        data = self._get(f"{self._company_url()}/{settings.bc_invoice_entity}", {
            "$filter": f"Order_No eq '{bc_po_no}'",
            "$select": F_NO, "$top": "1",
        })
        return "MATCHED" if data.get("value") else "PENDING_INVOICE"

    def post_sales_invoice(self, order: dict) -> str:
        raise NotImplementedError  # Phase 5
