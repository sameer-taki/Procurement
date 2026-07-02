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
| `DEFAULT_ROLE` | optional; role for users with no matching claim (default `VIEWER`) |

**App registration:** add the redirect URI above (type *Web*), enable ID tokens, and
either define **app roles** or emit **group** claims. The app maps the most-privileged
role *named* in that claim to a local role (`ADMIN`/`APPROVER`/`OFFICER`/`REQUESTER`/
`VIEWER`), case-insensitive. If your role/group names don't contain those words, send me
the exact names → values and I'll set an explicit mapping in `map_role`.

**Behaviour:** the "Sign in with Microsoft" button appears once configured; first SSO
login auto-provisions a local user (default `VIEWER`; an existing `ADMIN` is never
demoted). Verify: sign in via Microsoft → `/api/me` shows your mapped role.

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
| `BC_RECEIPT_ENTITY` | `PurchRcptHeaders` | posted-receipt number readback |
| `BC_INVOICE_ENTITY` | `PurchInvHeaders` | 3-way match (posted invoice = MATCHED) |
| `BC_VENDORS_ENTITY` | `Vendors` | vendor master sync |
| `BC_PURCHASE_PRICES_ENTITY` | `Purchase_Prices` | vendor price/MOQ sync |
| `BC_USAGE_ENTITY` | `ItemLedgerEntries` | usage export (SOP step 3) |
| `BC_RECEIPT_POST_ACTION` | `Microsoft.NAV.Post` | bound action that posts the receive |
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

### Paper planning: the usage export + grade/deckle

The Order Page (paper planning per the procurement SOP) needs two more reads:

| Env var | Value |
|---------|-------|
| `BC_USAGE_ENTITY` | usage-export entity set (default `ItemLedgerEntries`) |

* **Usage (SOP step 3):** `BCAdapter.get_usage_entries` reads the item ledger
  filtered to consumption (`Entry_Type eq 'Consumption'` — the postings Kiwiplan
  feeds back) and aggregates client-side to KG per item per month, upserted into
  `usage_history` via `POST /api/planning/import-usage`. **Confirm:** the entity
  name, the `Entry_Type` values that represent Kiwiplan job usage, and the field
  names `Item_No` / `Posting_Date` / `Quantity`.
* **Grade + deckle:** stock is planned by grade AND deckle (roll width). By
  default the adapter parses the item No against the grade-deckle SKU convention
  (`CWT140-1400`, regex in `BC_PAPER_SKU_REGEX`); if your BC carries them as item
  attributes instead, set `BC_GRADE_FIELD` / `BC_DECKLE_FIELD` and those win. A
  roll item that arrives with a deckle but **no grade** is excluded from planning
  and flagged on the Order Page.
* **Reconciliation (SOP §9):** `BCAdapter.get_inventory` reads BC's on-hand per
  item (`$select=No,Inventory` on the items entity) for the Order Page's
  BC-vs-production stock check (`GET /api/planning/reconciliation`). **Confirm:**
  that your items entity exposes the `Inventory` flowfield (a location-filtered
  Item_Ledger balance may be needed if BC tracks locations the app does not).

The BC usage import also runs on a schedule (default daily; `USAGE_IMPORT_ENABLED`
/ `USAGE_IMPORT_SECONDS`) so trailing averages stay current without the manual
"Import usage from BC" button — the SOP §9 cadence.

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
