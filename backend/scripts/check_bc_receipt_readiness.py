#!/usr/bin/env python3
"""Read-only readiness probe for live BC receipt posting.

Run this ON THE DOCKER HOST (10.1.1.234) — the only place that can reach BC on
the LAN — with the same env the app uses. It performs NO writes: it only reads,
to tell you three things before you set BC_RECEIPT_CORRELATION_FIELD and enable
live receipts:

  1. the configured BC account can connect + authenticate;
  2. the chosen correlation field (default Your_Reference) is QUERYABLE on the
     posted-receipt entity — the exactly-once pre-check filters on it, so it must
     exist there or the guard silently can't work;
  3. whether that field is already carrying real data on open purchase orders —
     if it is, DON'T repurpose it (pick another field) or you'll overwrite it.

It cannot prove WRITE permission without mutating BC; that last mile is the
one-line test receipt in INTEGRATIONS.md / the go-live guide. Usage:

    python -m scripts.check_bc_receipt_readiness [FIELD]     # FIELD default: Your_Reference

Exit code 0 = safe to proceed, 1 = something to fix first.
"""
import sys

from app.config import settings
from app.gateway.bc import BCAdapter, _odata_str

OK, WARN, BAD = "PASS", "WARN", "FAIL"


def main() -> int:
    field = sys.argv[1] if len(sys.argv) > 1 else "Your_Reference"
    problems = 0

    print(f"BC receipt-posting readiness — correlation field: {field}\n")

    if not settings.bc_enabled:
        print(f"[{BAD}] BC is not configured (BC_BASE_URL / BC_USERNAME / BC_PASSWORD).")
        print("       Run this on the Docker host with the app's env.")
        return 1

    adapter = BCAdapter()
    print(f"       base={settings.bc_base_url}  company={settings.bc_company}  "
          f"user={settings.bc_username}  auth={settings.bc_auth}\n")

    # 1. Connectivity + auth: cheapest possible read against the PO entity.
    po_url = f"{adapter._company_url()}/{settings.bc_po_entity}"
    try:
        adapter._get(po_url, {"$top": "1", "$select": "No"})
        print(f"[{OK}] Connected and authenticated to BC.")
    except Exception as exc:
        print(f"[{BAD}] Could not read {settings.bc_po_entity}: {exc}")
        print("       Fix connectivity/credentials before anything else.")
        return 1

    # 2. Is the correlation field queryable on the POSTED-RECEIPT entity?
    rcpt_url = f"{adapter._company_url()}/{settings.bc_receipt_entity}"
    try:
        adapter._get(rcpt_url, {"$top": "1", "$select": f"No,{field}",
                                "$filter": f"{field} eq 'PROBE-NONEXISTENT'"})
        print(f"[{OK}] '{field}' is queryable on {settings.bc_receipt_entity} "
              "(the exactly-once pre-check will work).")
    except Exception as exc:
        problems += 1
        print(f"[{BAD}] '{field}' is not queryable on {settings.bc_receipt_entity}: {exc}")
        print(f"       Pick a field that exists on the posted receipt, or expose "
              f"'{field}' on that page's web service.")

    # 3. Is the field already in use on open POs? (safety: we overwrite it.)
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
