# CLAUDE.md — Build guide for Golden Procurement

You (Claude Code) are building the **Golden Manufactures procurement app**. This file is your
contract: read it fully before writing code, and keep to its conventions, guardrails, and phase
plan. The repo already contains a working skeleton (deployment files, canonical schema, the BOM
engine, adapter stubs, a minimal UI). Your job is to flesh it out, phase by phase, without
breaking the deployment pattern.

---

## 1. What this app is

An internal web app for the procurement team: raise and track **requisitions**, get them
**approved** (tiered), turn them into **purchase orders**, **receive** goods, and see live
**stock**, **vendors**, **prices**, and **BOMs**. It also turns production demand into
**suggested purchasing** automatically.

It is the procurement team's daily workspace **and** the integration layer between three systems:
- **Business Central (BC)** — finance spine + masters: customer/vendor master, item master,
  **price per SKU** (price lists), posted POs, invoices, AP/AR/GL.
- **Kiwiplan** — corrugated box production: material requirements + roll-stock inventory.
- **Accura** — label production: label material requirements + label-stock inventory.

## 2. Architecture & the one rule

One deployable now. The **gateway** (the `backend/app/gateway/` package — adapters + canonical
models + BOM engine) lives *inside* this app behind a clean module boundary, so it can be lifted
into its own shared service later when a sales app is built. Do not split it yet.

**The rule that keeps it clean:** the gateway is the **only writer of canonical state**. External
systems are read for data and *report* status; this app decides every status transition and owns
the canonical tables. Ownership:
- **App owns:** requisition/approval/PO/receiving workflow, the canonical order/item/BOM tables,
  and the top "kit" level of cross-system BOMs.
- **BC owns:** money, **price/SKU**, customer/vendor/item masters, the *posted* PO and invoice.
- **Kiwiplan / Accura own:** production, **material BOMs**, and operational stock (execution truth).

This app **displays** inventory; it never becomes a competing source of stock truth. Mirror BOMs
read-only for planning/costing; read live per-order requirements from Kiwiplan/Accura as truth.

**Reachability:** running on the Docker host (10.1.1.234), adapters can reach BC (172.16.1.10),
Kiwiplan, and Accura directly over the LAN. (That on-prem reachability is the whole reason the
gateway lives in-app.)

## 3. Tech stack & conventions

- **Backend:** FastAPI on port **8000** (plain HTTP; Traefik terminates TLS). SQLModel +
  PostgreSQL 16 via `psycopg`. Config through `app/config.py` (pydantic-settings) — read every
  setting from env, never hardcode secrets.
- **Migrations:** add **Alembic** in Phase 1 and use it for all schema changes. Do not rely on
  `create_all` beyond throwaway local dev.
- **Frontend:** React + Vite, built into `frontend/dist` and served as static by FastAPI. Run
  `npm install` once and **commit `package-lock.json`** (CI uses `npm ci`).
- **Auth:** Entra ID OIDC (Authlib). Map Entra app-role/group claims to local roles
  (REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN). Approval limit lives on the role.
- **Tests:** every phase ships tests; `pytest -q` (backend) and `vitest run` (frontend) must stay
  green — CI gates merges to `main`.
- **API shape:** all endpoints under `/api`; keep routers in `app/domain/`. Use the canonical
  models in `app/gateway/models.py`; don't invent parallel schemas.
- **Money/stock are read from systems** and cached with an `as_of` timestamp; always show the user
  how fresh a figure is.

## 4. Repo map

```
backend/app/
  config.py            settings (env) — extend here, never hardcode
  main.py              FastAPI app, /health, static mount, router includes
  gateway/
    models.py          canonical schema (items, bom_*, vendors, requisitions, PO, stock, ...)
    bom.py             explode() / net_requirements() / round_to_moq() / explode_and_net()  ← engine
    bc.py              BCAdapter   (OData v4 / NTLM)  — implement the stubs
    kiwiplan.py        KiwiplanAdapter (KDW/SQL read, KMC inject) — implement the stubs
    accura.py          AccuraAdapter (ODBC read, web2print) — implement the stubs
  mailer.py            M365 Graph sendMail (functional; add token caching)
  auth/entra.py        OIDC SSO + RBAC (to build)
  domain/              your API routers + services per phase
frontend/              React UI (build Dashboard, Stock, Reqs, POs, Receiving, Vendors)
Dockerfile, docker-compose.traefik.yml, .env.example, .github/workflows/ci.yml
```

## 5. Deployment guardrails (DO NOT break — these caused real outages)

This app deploys via **Portainer GitOps** on the Golden host. Push to `main` → Portainer rebuilds
& redeploys at `https://procurement.gml.com.fj`. Keep all of this intact:

- Keep the label **`traefik.docker.network=web`** (missing it = gateway timeout).
- **No `ports:`** on the app or db service.
- FastAPI stays **plain HTTP on 8000**; do not add the `loadbalancer.server.scheme=https` lines.
- **`DB_PASSWORD` is permanent** once the volume exists; never rotate it in a way that locks out PG.
- Hostnames/labels use **`gml.com.fj`** (email identity is `no-reply@golden.com.fj` — that's
  correct, different domain on purpose).
- **Secrets only in Portainer env vars**, never committed. `.env.example` is documentation only.
- Don't rename the Portainer stack/volume.

## 6. Build phases (map onto KAN-37–42)

Work one phase at a time; open a PR per phase; keep CI green. Each phase has a Definition of Done.

### Phase 1 — Foundations + Stock view  *(read-only, ships value first)*
- Add Alembic; generate the initial migration from `models.py`; seed roles + first admin.
- Entra OIDC login (`/auth/login`, `/auth/callback`), signed session, `get_current_user`, RBAC
  dependency. VIEWER can see; only OFFICER/ADMIN mutate.
- Implement **read** paths in `BCAdapter.list_items` / `get_item_price`, `KiwiplanAdapter.get_stock`,
  `AccuraAdapter.get_stock`. Build a `stock` service that assembles the unified per-SKU view
  (on_hand · allocated · on_order · available, by location & system) into `stock_snapshots`, with
  a scheduled refresh (~15–30 min) **and** an on-demand "refresh this material".
- Frontend: app shell + auth gate + **Dashboard** and **Stock** screens (search any SKU → unified view).
- **DoD:** log in via Entra; search a SKU; see live stock with an `as_of`; `/health` green; CI green;
  deploys to `procurement.gml.com.fj`.

### Phase 2 — Requisitions + approval
- Requisition CRUD (header + lines), states DRAFT→SUBMITTED→IN_APPROVAL→APPROVED/REJECTED→CLOSED.
- **Tiered approval engine**: route by amount vs role `approval_limit` (+ cost-centre/category if
  needed). Record every transition in `order_events`.
- Frontend: raise a requisition, "approvals waiting on me", approve/reject.
- **DoD:** a requisition routes to the right approver and is fully audited.

### Phase 3 — PO posting + vendor email
- Approved requisition → **PurchaseOrder**; pick vendor via `vendor_prices` (price/lead/MOQ).
- Implement `BCAdapter.create_purchase_order`; on PO_ISSUED write to BC and store the BC PO no in
  `external_refs`. Use the **outbox** (`integration_outbox`) for reliable, retryable posting.
- Email the vendor via `mailer.send_mail` (Graph). 
- **DoD:** approving a req posts a real PO to BC and emails the vendor; failures retry, never double-post.

### Phase 4 — BOM + explosion service  *(the order→procurement bridge)*
- Item/BOM CRUD; mirror Kiwiplan/Accura material BOMs read-only (structure), keep the kit level in-app.
- Wire `bom.explode_and_net()` to the DB: `bom_of` from `bom_headers`/`bom_lines`, `stock` from
  `stock_snapshots`, `moq` from `vendor_prices`. On confirmed demand, emit **suggested requisitions**
  (status DRAFT, source=demand) for shortages.
- **DoD:** a demand signal produces correct suggested requisitions (covered by `test_bom.py`-style tests).

### Phase 5 — Receiving + 3-way match + analytics
- GRN capture against POs (PARTIALLY_RECEIVED / RECEIVED); push receipt to BC; let BC do the
  3-way match (PO·GRN·invoice → MATCHED). Update stock.
- Feed spend / on-time-delivery / stock-turn data to the Azure SQL warehouse for Power BI.
- **DoD:** receive against a PO, see it matched in BC, and the figures land in the warehouse.

## 7. Before you start — confirm with the user (don't guess)

- **Kiwiplan**: which inbound channel (KMC / Transmission Links) and which requirement/stock views
  your licence exposes via KDW/SQL.
- **Accura**: whether a supported inbound job-creation interface exists (gates label automation),
  and the ODBC read shape for stock/requirements.
- **Approval thresholds** and whether the app owns min/max for indirect/MRO items.
- **BC**: exact OData price-list entity + auth (NavUserPassword vs NTLM) for `get_item_price`.

When a real-world interface is unknown, build against a small adapter interface + a fake, leave a
clear `TODO`, and surface the open question — do not invent a vendor API.

## 8. Local dev

```
docker run -d --name pg -e POSTGRES_USER=fmp -e POSTGRES_PASSWORD=fmp -e POSTGRES_DB=fmp -p 5432:5432 postgres:16-alpine
cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev   # then commit package-lock.json
```

Keep changes small, tested, and behind the module boundary. Ask before anything irreversible
(deleting data, rotating `DB_PASSWORD`, changing infra/Portainer).
