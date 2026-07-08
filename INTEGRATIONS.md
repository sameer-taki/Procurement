# Wiring the live systems

Every external source is **config-gated**: with nothing set it serves clearly-badged
**demo** data; it switches to **live** automatically once you provide the full set of
settings below. Set these as **Portainer env vars** — never commit secrets. No code
changes are needed to go live (except confirming a couple of names, noted per system).

A source only flips to live when it has *everything* it needs, so partial config
safely stays in demo mode.

---

## 1. Entra ID SSO  (closest to ready)

| Env var | Value |
|---------|-------|
| `ENTRA_TENANT_ID` | your tenant GUID |
| `ENTRA_CLIENT_ID` | the app registration's client ID |
| `ENTRA_CLIENT_SECRET` | client secret (Portainer only) |
| `ENTRA_REDIRECT_URI` | `https://procurement.gml.com.fj/auth/callback` |
| `ENTRA_ROLE_CLAIM` | optional; defaults to `roles` (use `groups` if you map by group) |
| `ENTRA_ROLE_MAP` | optional JSON: `{"<claim value or GUID>": "<local role>"}` — the recommended production mapping |
| `DEFAULT_ROLE` | optional; role for users with no matching claim (default `VIEWER`) |

**App registration:** add the redirect URI above (type *Web*), enable ID tokens, and
either define **app roles** or emit **group** claims. Role resolution is **exact**, never
substring: each claim value is matched by `ENTRA_ROLE_MAP` (an explicit value→role map,
the production path) or, when the map is empty, by a whole-token match of the local role
code (`ADMIN`/`APPROVER`/`OFFICER`/`REQUESTER`/`VIEWER`, case-insensitive). Exact matching
is deliberate so a group like `Finance-Admins` can't accidentally grant `ADMIN`. To map
opaque group GUIDs, set `ENTRA_ROLE_MAP` — no code change needed. The most-privileged
matched role wins.

**Behaviour:** the "Sign in with Microsoft" button appears once configured; first SSO
login auto-provisions a local user (`DEFAULT_ROLE`). On re-login, a token that **carries**
the role claim overwrites the local role (so an admin revoked in Entra loses it here too);
a token with **no** role claim leaves the current role untouched. The seeded local admin
(admin-login, no Entra link) is unaffected. Verify: sign in via Microsoft → `/api/me`
shows your mapped role.

---

## 2. Business Central  (on-prem; validated on deploy)

| Env var | Value |
|---------|-------|
| `BC_BASE_URL` | e.g. `https://172.16.1.10:7048/BC/ODataV4` |
| `BC_COMPANY` | company name used in the OData URL |
| `BC_USERNAME` / `BC_PASSWORD` | service account |
| `BC_AUTH` | `ntlm` (default) or `basic` (NavUserPassword) |
| `BC_VERIFY_TLS` | `false` only for a self-signed on-prem cert |
| `BC_ITEMS_ENTITY` | item-master entity set (default `Items`) |

**The full live loop is built** — item master, price list, vendors, usage, PO
posting (header + lines, retry-safe), receipt posting (Qty_to_Receive + post
action) and the invoice-based 3-way-match signal all speak standard BC V4
page-based OData. Every entity set, field name and mapping convention is a
setting, so pointing the adapter at your tenant is an **env-var exercise, not a
code change**. The defaults (override any that differ):

| Env var | Default | Used for |
|---------|---------|----------|
| `BC_ITEMS_ENTITY` | `Items` | item master + `Inventory` reconciliation read |
| `BC_PO_ENTITY` | `PurchaseOrders` | PO header create + receive post action |
| `BC_PO_LINES_ENTITY` | `PurchaseOrderLines` | PO line posting, `Qty_to_Receive` |
| `BC_PO_EXTREF_FIELD` | `External_Document_No` | PO header field holding the app's PO no. (idempotent find-or-create); **BC14 lacks this on the PO page — use `Vendor_Order_No`** |
| `BC_RECEIPT_ENTITY` | `PurchRcptHeaders` | posted-receipt number readback |
| `BC_INVOICE_ENTITY` | `PurchInvHeaders` | 3-way match (posted invoice = MATCHED) |
| `BC_VENDORS_ENTITY` | `Vendors` | vendor master sync |
| `BC_CUSTOMERS_ENTITY` | `Customers` | customer master sync (forecast picker) |
| `BC_PURCHASE_PRICES_ENTITY` | `Purchase_Prices` | vendor price/MOQ sync |
| `BC_USAGE_ENTITY` | `ItemLedgerEntries` | usage export (SOP step 3) |
| `BC_RECEIPT_POST_ACTION` | `Microsoft.NAV.Post` | bound action that posts the receive |
| `BC_RECEIPT_CORRELATION_FIELD` | *(blank)* | order-header field stamped with the grn_no for **exactly-once** receipt posting (e.g. `Vendor_Shipment_No`); blank = best-effort |
| `BC_REORDER_POINT_FIELD` | `Reorder_Point` | item reorder point (0 = unset) |
| `BC_LEAD_TIME_FIELD` | `Lead_Time_Calculation` | dateformula → days (`45D`/`2W`/`1M`) |
| `BC_REPLENISHMENT_FIELD` | *(blank)* | e.g. `Replenishment_System`; `Prod. Order` ⇒ FINISHED |

Standard shared fields `No` / `Description` / `Base_Unit_of_Measure` /
`Unit_Price` / `Inventory` / `Name` / `E_Mail` / `Item_No` / `Vendor_No` /
`Direct_Unit_Cost` / `Minimum_Quantity` are constants at the top of `bc.py` —
the single place to change if your tenant renames them. All reads follow
`@odata.nextLink` pagination.

**Retry-safety:** the app's PO number rides in `External_Document_No`, so a
retried post finds the existing BC document and completes its lines instead of
duplicating the header; receipts sum `Qty_to_Receive` per item and never re-post
a GRN that already has its crosswalk.

**Exactly-once receipts (go-live gate for live receipt writes):** the app-level
crosswalk guard can't cover the window where BC's *Post* action succeeds but its
HTTP response is lost — a retry would then re-receive. Closing that needs a
correlation key **inside BC**. Set `BC_RECEIPT_CORRELATION_FIELD` to an
order-header field BC copies onto the posted receipt (standard BC exposes
`Vendor_Shipment_No` on both the Purchase Order and the Purch. Rcpt. Header —
confirm it's free to repurpose as a key on your tenant). With it set, the adapter
stamps the field with the canonical `grn_no` before posting and, on every attempt,
first asks BC whether a receipt already carries that `grn_no`; if so it returns
that receipt and posts nothing. Leave it **blank** and receipt posting stays
best-effort (the documented double-post risk) — run receipts with a read-only BC
account until the field is confirmed and set. PO posting has no such gap and is
safe to enable regardless.

### Paper planning: the usage export + grade/deckle

The Order Page (paper planning per the procurement SOP) needs two more reads.
**Verified against GML's live test server (bc-test.gml.com.fj = 172.16.1.10,
BC14 on-prem, instance `BC140`, http on 7048):**

| Env var | Value |
|---------|-------|
| `BC_BASE_URL` | `http://172.16.1.10:7048/BC140/ODataV4` (test box; prod TBD) |
| `BC_COMPANY` | `Golden Manufacturers Pte Ltd` |
| `BC_AUTH` | `ntlm` (domain accounts, `GML\<user>`) |
| `BC_USAGE_ENTITY` | `ItemLedgerEntries` — already published (as a Query object) |
| `BC_USAGE_ENTRY_TYPES` | default `Negative Adjmt.,Consumption` — Kiwiplan job usage posts as **Negative Adjmt.**; 'Consumption' returned nothing on this tenant |

* **Usage (SOP step 3):** `BCAdapter.get_usage_entries` reads the item ledger
  filtered to the `BC_USAGE_ENTRY_TYPES` since the trailing window start and
  aggregates to KG per item per month (`POST /api/planning/import-usage`).
  Field names confirmed on BC140: `Item_No` / `Posting_Date` ('YYYY-MM-DD') /
  `Quantity` (signed) / `Entry_Type`. The service is a QUERY object:
  `$filter`/`$select`/`$top` work, `$orderby` does not (the adapter never uses
  it). The import also runs on a schedule (default daily;
  `USAGE_IMPORT_ENABLED` / `USAGE_IMPORT_SECONDS`) so trailing averages stay
  current without the manual button — the SOP §9 cadence.
* **Item master:** on-prem BC only exposes published services — publish
  **Page 31 as service name `Items`** (Web Services page in the client).
* **Customer master:** likewise publish **Page 22 as service name `Customers`**
  (Web Services page). The adapter reads `No` / `Name` / `E_Mail` and syncs into
  the app's `customers` table (BC-owned, read-only in the app), which feeds the
  Forecasts customer picker and the Customers screen. Until it is published the
  app serves the demo customer list.
* **Grade + deckle:** stock is planned by grade AND deckle (roll width). The
  adapter reads explicit fields when `BC_GRADE_FIELD` / `BC_DECKLE_FIELD` are
  set; otherwise it parses the item No against `BC_PAPER_SKU_REGEX`. The deckle
  regex group is optional — **on GML's tenant the item No IS the grade**
  (`WTL175`, `BX186`; one item per grade, no deckle suffix), so go-live needs a
  grade-only pattern, e.g. `BC_PAPER_SKU_REGEX=^([A-Z]{2,4}\d{3})$` scoped to
  the real grade shapes. Where deckle lives (lot no. / variant / Kiwiplan-only)
  is still to be confirmed; until then live paper plans per grade. A roll item
  that arrives with a deckle but **no grade** is excluded from planning and
  flagged on the Order Page.
* **Reconciliation (SOP §9):** `BCAdapter.get_inventory` reads BC's on-hand per
  item (`$select=No,Inventory` on the items entity) for the Order Page's
  BC-vs-production stock check (`GET /api/planning/reconciliation`). **Confirm:**
  that your items entity exposes the `Inventory` flowfield (a location-filtered
  Item_Ledger balance may be needed if BC tracks locations the app does not).

---

## 3. Kiwiplan stock  (on-prem; validated on deploy)

| Env var | Value |
|---------|-------|
| `KIWIPLAN_DSN` | ODBC connection string for KDW/SQL |
| `KIWIPLAN_STOCK_SQL` | a query returning `location, on_hand, allocated, on_order` with one `:item_ref` placeholder |

Example (replace table/columns with your KDW view):
```sql
SELECT warehouse AS location, qty_on_hand AS on_hand,
       qty_allocated AS allocated, qty_on_order AS on_order
FROM   kdw_material_stock
WHERE  material_code = :item_ref
```
The adapter runs this, mapping result columns by name. Confirm the real view/columns
with Advantive (CLAUDE.md §7).

---

## 4. Accura stock  (on-prem; validated on deploy)

Same shape as Kiwiplan: set `ACCURA_DSN` and `ACCURA_STOCK_SQL` (a `:item_ref`
query returning `location/on_hand/allocated/on_order`). Confirm the ODBC table/columns
with Data Design Services.

---

## 5. Vendor email (Phase 3, optional now)

`GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_SENDER`
(`no-reply@golden.com.fj`) — the existing "Golden Apps Mailer" registration.

---

## 6. Azure SQL analytics warehouse (Phase 5)

| Env var | Value |
|---------|-------|
| `AZURE_SQL_DSN` | a full pyodbc connection string for the Azure SQL warehouse |

Power BI reads the procurement KPIs (spend, on-time-delivery, stock-turn) from an
Azure SQL warehouse. The app **computes** those figures from its own canonical data
(receipts, PO lines, stock snapshots) and **pushes** them via `POST /api/analytics/push`
(ADMIN). The warehouse is a read-only analytics **sink** — never canonical state; the
gateway stays the only writer of canonical tables.

**Guarded, like every other integration:** with no `AZURE_SQL_DSN` set, the writer
(`gateway/warehouse.py`) logs and no-ops, returning `skipped:not-configured` per table
and never raising — so the push endpoint is safe to call in demo mode. Set the DSN
(Portainer env only; never commit it) to flip it live. `WAREHOUSE_DSN` is accepted as
an alias of `AZURE_SQL_DSN`.

**Still open (tell me and I'll wire):** the warehouse table schema — one table per
metric (`spend` / `on_time_delivery` / `stock_turn`) vs a single tall fact table keyed
by metric + `as_of` — and the upsert strategy (the live `_write` is a parameterized
INSERT skeleton; a MERGE-by-`as_of` is the likely production shape). Also confirm the
Azure SQL ODBC driver name on the Docker host.

---

## Important: the cross-system crosswalk

The Stock view fetches a material's Kiwiplan/Accura stock using `item.kiwiplan_ref` /
`item.accura_ref`. The mapping is configurable via `CROSSWALK_MODE`:

| Mode | Behaviour |
|------|-----------|
| `sku` *(default)* | the BC item No IS the material code in Kiwiplan/Accura (a stock read for a code a system doesn't carry just returns no rows) |
| `fields` | read the refs from the item-master OData fields named in `BC_KIWIPLAN_REF_FIELD` / `BC_ACCURA_REF_FIELD` |
| `none` | leave refs unset — per-material stock stays empty until mapped |

If the codes don't line up 1:1 across your three systems, switch to `fields` (or
tell me the convention and I'll add it). This is the last piece for end-to-end
live stock.
