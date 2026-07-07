# Golden Procurement

Internal procurement app for Golden Manufactures: requisitions, approvals, POs,
receiving, vendor + stock visibility, BOM/SKU, and demand-driven purchasing.
Integrates with Business Central (price/SKU, masters, PO, invoice), Kiwiplan
(box production + stock), and Accura (labels + stock) through an internal gateway.

- **Build guide for Claude Code:** see `CLAUDE.md` (read it first).
- **Deploy:** Portainer GitOps on the Golden host — see `DEPLOY.md`. Merge to `main`
  -> Portainer rebuilds & redeploys at `https://procurement.gml.com.fj`.

## Status
All six build phases are shipped (deployed at `procurement.gml.com.fj`):
- **Phase 1 — Foundations + Stock view:** Alembic migrations + role/admin seed,
  signed-session auth (bootstrap admin login + Entra OIDC + RBAC), unified
  per-SKU Stock view from BC/Kiwiplan/Accura adapters, Dashboard + Stock UI.
- **Phase 2 — Requisitions + tiered approval** (routed by amount vs role limit).
- **Phase 3 — PO posting to BC + vendor email** via a reliable, idempotent outbox.
- **Phase 4 — BOM explosion → suggested requisitions** (board-grade spec CRUD).
- **Phase 5 — Receiving + 3-way match + analytics warehouse push.**
- **Phase 6 — Paper planning:** 3-month cover by grade/deckle, forecasts (cartons)
  exploded to KG, BC usage import, 25 t/FCL consolidation, shipment tracking.
- Plus an Admin panel (users/roles/system health) and exportable operational Reports.

BC is wired live against the on-prem tenant; unconfigured sources still serve
clearly-flagged demo data (see CLAUDE.md §7 / INTEGRATIONS.md for the remaining
tenant-specific confirmations).

## Local dev
    # 1) DB — Postgres, or just use SQLite for quick dev (export DATABASE_URL=sqlite:///dev.db)
    docker run -d --name pg -e POSTGRES_USER=fmp -e POSTGRES_PASSWORD=fmp -e POSTGRES_DB=fmp -p 5432:5432 postgres:16-alpine
    # 2) backend (Alembic migrations + role/admin seed + demo data run on startup)
    cd backend && pip install -r requirements.txt
    SECRET_KEY=dev-secret-key-0123456789-0123456789 APP_ENV=dev uvicorn app.main:app --reload
    # 3) frontend (dev server proxies /api + /auth to :8000)
    cd frontend && npm install && npm run dev
    #    sign in with ADMIN_USERNAME/ADMIN_PASSWORD (default admin / admin)

Backend tests: `cd backend && pytest -q` (SQLite, no Postgres needed).
Frontend tests/build: `cd frontend && npm test && npm run build`.

`frontend/package-lock.json` is committed (CI uses `npm ci`).
