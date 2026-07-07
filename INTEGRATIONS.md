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

**Confirm:** the entity name and that the standard fields `No`, `Description`,
`Base_Unit_of_Measure`, `Unit_Price` exist (they're the defaults the adapter reads;
they're the single place in `bc.py` to change if yours differ). `list_items` follows
OData `@odata.nextLink` pagination; price comes from `Unit_Price`.

**Still open (tell me and I'll wire):** how to derive `item_type` from your item
categories, `reorder_point`/`lead_time`, and — important — the **crosswalk** from a BC
item to its Kiwiplan/Accura material id (see note below).

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
  filtered to those entry types since the trailing window start and aggregates
  to KG per item per month (`POST /api/planning/import-usage`). Field names
  confirmed on BC140: `Item_No` / `Posting_Date` ('YYYY-MM-DD') / `Quantity`
  (signed) / `Entry_Type`. The service is a QUERY object: `$filter`/`$select`/
  `$top` work, `$orderby` does not (the adapter never uses it).
* **Item master:** on-prem BC only exposes published services — publish
  **Page 31 as service name `Items`** (Web Services page in the client).
* **Grade + deckle:** BC item numbers ARE the grade (`WTL175`, `BX186`) — one
  item per grade, no deckle in the item number. Where deckle lives (lot no. /
  variant / Kiwiplan-only) is still to be confirmed before `_map_item` fills
  `items.deckle_mm`; grade can map from the item no. / category once the paper
  category code is known.

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
`item.accura_ref`. In **demo** mode those refs are set for you. In **live** mode, BC's
item master doesn't yet tell us which BC item corresponds to which Kiwiplan/Accura
material — that mapping is an open decision. So even with BC + Kiwiplan + Accura all
configured, per-material stock stays empty until we populate those refs (e.g. a naming
convention, a BC field, or a small mapping table). Tell me how the codes line up across
your three systems and I'll wire the crosswalk — that's the last piece for end-to-end
live stock.
