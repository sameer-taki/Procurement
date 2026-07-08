#!/usr/bin/env python3
"""Read-only readiness probe for live BC purchase-document writes (PO + receipt).

Run this ON THE DOCKER HOST — inside the app container, which has the deps and
the BC env — the only place that can reach BC on the LAN. It performs NO writes.
It tells you, before you enable live PO/receipt posting:

  1. the configured BC account connects + authenticates;
  2. which entity sets BC actually has PUBLISHED (on-prem BC only exposes
     published web services), and whether the ones the app writes to — the
     purchase order, its lines, posted receipts, posted invoices — are among
     them under the names the app is configured to use;
  3. the chosen correlation field (default Your_Reference) is QUERYABLE on the
     posted-receipt entity — the exactly-once pre-check filters on it;
  4. whether that field already carries real data on open POs — if it does,
     DON'T repurpose it (pick another) or you'll overwrite it.

It cannot prove WRITE permission without mutating BC; that last mile is the
one-line test receipt in the go-live guide. Usage:

    docker exec -it procurement-app-1 \\
        python -m scripts.check_bc_receipt_readiness [FIELD]   # default Your_Reference

Exit code 0 = safe to proceed, 1 = something to fix first.
"""
import sys

from app.config import settings
from app.gateway.bc import BCAdapter

OK, WARN, BAD = "PASS", "WARN", "FAIL"


def _published_entity_sets(adapter) -> set:
    """Names of every entity set the OData service root exposes (= published
    web services on on-prem BC). Raising here means auth/connectivity failed."""
    doc = adapter._get(settings.bc_base_url)
    return {e.get("name") for e in doc.get("value", []) if e.get("name")}


def _string_props(adapter, entity_type_name: str) -> set:
    """String property names actually exposed on an entity, read from BC's
    $metadata (works even when the entity has zero rows). A page web service
    only exposes fields placed on its layout, so this is the true field list —
    the menu of candidate correlation fields.

    Cached on the function so PO + receipt lookups share one metadata fetch."""
    import xml.etree.ElementTree as ET

    import requests

    cache = getattr(_string_props, "_doc", None)
    if cache is None:
        url = f"{settings.bc_base_url.rstrip('/')}/$metadata"
        r = requests.get(url, auth=adapter._auth(),
                         verify=settings.bc_verify_tls, timeout=60)
        r.raise_for_status()
        cache = ET.fromstring(r.content)
        _string_props._doc = cache

    out = set()
    for et in cache.iter():
        if et.tag.endswith("}EntityType") and et.get("Name") == entity_type_name:
            for p in et:
                if (p.tag.endswith("}Property") and p.get("Name")
                        and p.get("Type") == "Edm.String"):
                    out.add(p.get("Name"))
    return out


def _print_correlation_candidates(adapter) -> None:
    """List string fields present on BOTH the PO and posted-receipt entities —
    the valid choices for BC_RECEIPT_CORRELATION_FIELD. Never raises."""
    try:
        po = _string_props(adapter, settings.bc_po_entity)
        rc = _string_props(adapter, settings.bc_receipt_entity)
    except Exception as exc:
        print(f"       (couldn't read $metadata to list candidates: {exc})")
        return
    both = sorted(po & rc)
    if not both:
        print("       (no string field is exposed on BOTH entities — a field may "
              "need adding to the page layouts)")
        return
    # Surface reference-ish names first; those are the natural correlation keys.
    hint = ("reference", "vendor", "shipment", "external", "order", "invoice", "no")
    likely = [f for f in both if any(h in f.lower() for h in hint)]
    print("       Candidate correlation fields (string, on BOTH PO + receipt):")
    print(f"         likely: {likely or '(none obvious)'}")
    print(f"         all:    {both}")


def main() -> int:
    # Field to test: CLI arg > the configured env value > a sensible default.
    field = (sys.argv[1] if len(sys.argv) > 1
             else settings.bc_receipt_correlation_field or "Your_Reference")
    problems = 0

    print(f"BC write-path readiness — correlation field: {field}\n")

    if not settings.bc_enabled:
        print(f"[{BAD}] BC is not configured (BC_BASE_URL / BC_USERNAME / BC_PASSWORD).")
        print("       Run this inside the app container with the app's env.")
        return 1

    adapter = BCAdapter()
    print(f"       base={settings.bc_base_url}  company={settings.bc_company}  "
          f"user={settings.bc_username}  auth={settings.bc_auth}\n")

    # 1. Connectivity + auth + discovery: read the OData service document.
    try:
        published = _published_entity_sets(adapter)
        print(f"[{OK}] Connected and authenticated; BC exposes "
              f"{len(published)} published entity set(s).")
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        print(f"[{BAD}] Could not read the OData service root: {exc}")
        if status == 401:
            print("       401 = the account can't authenticate — credential/permission issue.")
        else:
            print("       Check BC_BASE_URL and that OData/web services are enabled.")
        return 1

    # 2. Are the entity sets the app WRITES to actually published, as named?
    required = {
        "purchase order (BC_PO_ENTITY)": settings.bc_po_entity,
        "purchase order lines (BC_PO_LINES_ENTITY)": settings.bc_po_lines_entity,
        "posted receipts (BC_RECEIPT_ENTITY)": settings.bc_receipt_entity,
        "posted invoices (BC_INVOICE_ENTITY)": settings.bc_invoice_entity,
    }
    missing = []
    for label, name in required.items():
        if name in published:
            print(f"[{OK}] {label}: '{name}' is published.")
        else:
            problems += 1
            missing.append(name)
            print(f"[{BAD}] {label}: '{name}' is NOT published.")
    if missing:
        near = sorted(n for n in published
                      if any(k in n.lower() for k in ("purch", "receipt", "rcpt", "order", "invoice")))
        print("       Publish the matching page(s) as web services (BC → Web Services), "
              "or point the BC_*_ENTITY env var at the published name.")
        if near:
            print(f"       Purchase-ish entity sets already published: {near}")
        else:
            print("       (No obviously purchase-related entity sets are published yet.)")

    # 3/4 need the posted-receipt entity to exist; skip cleanly if it doesn't.
    if settings.bc_receipt_entity not in published:
        print(f"\n{problems} issue(s) to resolve before enabling live receipts.")
        return 1

    adapter_company = adapter._company_url()
    po_url = f"{adapter_company}/{settings.bc_po_entity}"
    rcpt_url = f"{adapter_company}/{settings.bc_receipt_entity}"
    po_published = settings.bc_po_entity in published

    def sample_po(fieldname: str, purpose: str) -> int:
        """Report whether fieldname already holds real data on existing POs (we
        overwrite it, so it must be free). Returns 1 if it's in use, else 0."""
        if not po_published:
            return 0
        try:
            rows = adapter._get(po_url, {"$top": "20", "$select": f"No,{fieldname}"}).get("value", [])
        except Exception as exc:
            print(f"[{WARN}] Couldn't sample '{fieldname}': {str(exc).split(' for url:')[0]}")
            return 0
        used = [r for r in rows if str(r.get(fieldname) or "").strip()]
        if not rows:
            print(f"[{WARN}] No POs to sample — can't confirm '{fieldname}' ({purpose}) is free.")
            return 0
        if used:
            eg = ", ".join(f"{r.get('No')}={r.get(fieldname)!r}" for r in used[:5])
            print(f"[{BAD}] '{fieldname}' ({purpose}) holds real data on {len(used)}/{len(rows)} "
                  f"sampled POs — don't repurpose it. e.g. {eg}")
            return 1
        print(f"[{OK}] '{fieldname}' ({purpose}) is blank on all {len(rows)} sampled POs — safe.")
        return 0

    # The two tag fields must differ: one holds the PO number, the other the GRN.
    if field and field == settings.bc_po_extref_field:
        problems += 1
        print(f"[{BAD}] correlation field and BC_PO_EXTREF_FIELD are both '{field}' — "
              "they must be different fields.")

    # PO idempotency tag (BC_PO_EXTREF_FIELD): queryable + free on the PO page.
    extref = settings.bc_po_extref_field
    if po_published:
        try:
            adapter._get(po_url, {"$top": "1", "$select": f"No,{extref}"})
            print(f"[{OK}] PO ext-ref field '{extref}' is queryable on {settings.bc_po_entity}.")
            problems += sample_po(extref, "PO idempotency tag")
        except Exception as exc:
            problems += 1
            print(f"[{BAD}] PO ext-ref field '{extref}' not queryable on {settings.bc_po_entity}: "
                  f"{str(exc).split(' for url:')[0]}")
            try:
                print("       Set BC_PO_EXTREF_FIELD to a string field exposed on the PO:")
                print(f"         {sorted(_string_props(adapter, settings.bc_po_entity))}")
            except Exception:
                pass

    # Receipt exactly-once correlation field: queryable on the receipt + free on the PO.
    try:
        adapter._get(rcpt_url, {"$top": "1", "$select": f"No,{field}",
                                "$filter": f"{field} eq 'PROBE-NONEXISTENT'"})
        print(f"[{OK}] correlation field '{field}' is queryable on {settings.bc_receipt_entity}.")
        problems += sample_po(field, "receipt correlation key")
    except Exception as exc:
        problems += 1
        print(f"[{BAD}] correlation field '{field}' not queryable on {settings.bc_receipt_entity}: "
              f"{str(exc).split(' for url:')[0]}")
        print(f"       Pick a field on BOTH PO + receipt (see below), or add '{field}' to the receipt layout.")
        _print_correlation_candidates(adapter)

    print()
    if problems:
        print(f"{problems} issue(s) to resolve before enabling live receipts.")
        return 1
    print("Reads look good. Remaining step is the one-line test receipt to prove "
          "write permission end to end (see the go-live guide).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
