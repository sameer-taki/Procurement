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
    field = sys.argv[1] if len(sys.argv) > 1 else "Your_Reference"
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
    rcpt_url = f"{adapter_company}/{settings.bc_receipt_entity}"
    try:
        adapter._get(rcpt_url, {"$top": "1", "$select": f"No,{field}",
                                "$filter": f"{field} eq 'PROBE-NONEXISTENT'"})
        print(f"[{OK}] '{field}' is queryable on {settings.bc_receipt_entity} "
              "(the exactly-once pre-check will work).")
    except Exception as exc:
        problems += 1
        short = str(exc).split(" for url:")[0]
        print(f"[{BAD}] '{field}' is not queryable on {settings.bc_receipt_entity}: {short}")
        print(f"       Pick a field that exists on the posted receipt (see below), or "
              f"add '{field}' to that page's layout.")
        _print_correlation_candidates(adapter)

    po_url = f"{adapter_company}/{settings.bc_po_entity}"
    if settings.bc_po_entity in published:
        try:
            data = adapter._get(po_url, {"$top": "20", "$select": f"No,{field}"})
            rows = data.get("value", [])
            used = [r for r in rows if str(r.get(field) or "").strip()]
            if not rows:
                print(f"[{WARN}] No purchase orders to sample — can't tell if '{field}' is used.")
            elif used:
                problems += 1
                sample = ", ".join(f"{r.get('No')}={r.get(field)!r}" for r in used[:5])
                print(f"[{BAD}] '{field}' is populated on {len(used)}/{len(rows)} sampled POs "
                      f"— it holds real data. Do NOT repurpose it. e.g. {sample}")
            else:
                print(f"[{OK}] '{field}' is blank on all {len(rows)} sampled POs "
                      "— safe to repurpose as the correlation key.")
        except Exception as exc:
            print(f"[{WARN}] Could not sample '{field}' on POs: {exc}")

    print()
    if problems:
        print(f"{problems} issue(s) to resolve before enabling live receipts.")
        return 1
    print("Reads look good. Remaining step is the one-line test receipt to prove "
          "write permission end to end (see the go-live guide).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
