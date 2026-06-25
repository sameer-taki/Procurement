# Golden Procurement

Internal procurement app for Golden Manufactures: requisitions, approvals, POs,
receiving, vendor + stock visibility, BOM/SKU, and demand-driven purchasing.
Integrates with Business Central (price/SKU, masters, PO, invoice), Kiwiplan
(box production + stock), and Accura (labels + stock) through an internal gateway.

- **Build guide for Claude Code:** see `CLAUDE.md` (read it first).
- **Deploy:** Portainer GitOps on the Golden host. Push to `main` -> Portainer
  rebuilds & redeploys at `https://procurement.gml.com.fj`.

## Local dev
    # 1) DB
    docker run -d --name pg -e POSTGRES_USER=fmp -e POSTGRES_PASSWORD=fmp -e POSTGRES_DB=fmp -p 5432:5432 postgres:16-alpine
    # 2) backend
    cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload
    # 3) frontend
    cd frontend && npm install && npm run dev

## First-time note
CI uses `npm ci`, which needs a committed `frontend/package-lock.json`.
Run `npm install` once in `frontend/` and commit the lockfile before the first CI run.
