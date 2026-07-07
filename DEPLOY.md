# Deploying Golden Procurement (Portainer GitOps)

The app deploys via **Portainer GitOps** on the Golden host. Portainer watches a
branch of this repo, rebuilds the image from `Dockerfile`, and redeploys the stack
defined in `docker-compose.traefik.yml`. Traefik publishes it at
`https://procurement.gml.com.fj`.

> The build (multi-stage: React → static, then FastAPI) and the Postgres pull both
> happen **on the Golden host**, which has LAN + internet egress. They cannot be
> built from a Claude Code cloud session (its egress policy blocks container
> registries), so everything below is host-side setup.

## 1. Host prerequisites (already in place for other Golden apps)

- Docker + Portainer running on the host (`10.1.1.234`).
- Traefik attached to an **external** Docker network named `web`, terminating TLS
  and routing by Host header. (This compose joins `web`; do not remove the
  `traefik.docker.network=web` label or the app will time out.)
- DNS: `procurement.gml.com.fj` → the host.

## 2. Create the stack in Portainer (first time)

1. **Stacks → Add stack → Git repository.**
2. Repository URL: this repo. **Reference:** `refs/heads/main`.
   (GitOps watches `main`. We develop on feature branches and **merge to `main`
   to release** — see §6.)
3. **Compose path:** `docker-compose.traefik.yml`
4. Enable **GitOps updates** (polling or webhook) so pushes to `main` redeploy.
5. Add the environment variables below, then **Deploy the stack**.
   (Don't rename the stack or its `pgdata` volume once created.)

## 3. Environment variables (set in Portainer — never commit secrets)

**Required** (the compose fails fast if any are missing):

| Var | Notes |
|-----|-------|
| `APP_HOST` | `procurement.gml.com.fj` (Traefik Host rule) |
| `DB_PASSWORD` | Strong random. **Permanent for the life of the `pgdata` volume** — never rotate in a way that locks out Postgres. |
| `SECRET_KEY` | 32+ chars, random. The app refuses to start in production with the placeholder or anything shorter. Signs the session cookie. |
| `ADMIN_USERNAME` | Bootstrap admin login (e.g. `admin`). |
| `ADMIN_PASSWORD` | Strong. This is the break-glass admin password — change it from anything default. |

Generate secrets:
```
docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Optional — add when each phase / integration goes live** (safe to leave blank;
until set, that source shows clearly-flagged demo data and SSO stays off):

| Var(s) | Enables |
|--------|---------|
| `APP_ENV` | defaults to `production`; leave as-is |
| `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI` | Entra ID SSO. Set redirect to `https://procurement.gml.com.fj/auth/callback` and register the same URI in the Entra app. |
| `BC_BASE_URL`, `BC_COMPANY`, `BC_USERNAME`, `BC_PASSWORD`, `BC_AUTH` | Live Business Central (item master, price, PO, usage). See INTEGRATIONS.md §2 for the full BC var set (`BC_PAPER_SKU_REGEX`, `BC_USAGE_ENTRY_TYPES`, entity overrides). |
| `KIWIPLAN_DSN` **+** `KIWIPLAN_STOCK_SQL` | Live Kiwiplan stock read — needs **both** the DSN and the parameterised query; the DSN alone stays in demo mode |
| `ACCURA_DSN` **+** `ACCURA_STOCK_SQL` | Live Accura stock read — needs **both** (see INTEGRATIONS.md §3–4) |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_SENDER` | Vendor email (Phase 3) |
| `BACKUP_KEEP_DAYS/WEEKS/MONTHS` | DB backup retention |

## 4. What happens on startup

On boot the app runs **Alembic migrations** (`alembic upgrade head`), then seeds the
five roles (REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN) and the bootstrap admin
user. On a fresh DB with no integrations configured it also loads a small demo
catalog so the Stock view is immediately usable. A background job refreshes stock
every ~30 min; users can also "refresh this material" on demand.

## 5. First login & verifying the deploy

1. Open `https://procurement.gml.com.fj` → sign in with `ADMIN_USERNAME` /
   `ADMIN_PASSWORD`.
2. You should see the **Dashboard** (item/material counts, below-reorder list,
   source-system modes) and **Stock** (search any SKU → unified on-hand · allocated ·
   on-order · available by system & location, with an `as_of`).
3. Health check: `GET /health` → `{"status":"ok"}`.
4. Until `ENTRA_*` is set, sources show a **DEMO** badge. Once configured, the
   "Sign in with Microsoft" button appears and live data replaces demo per source.

## 6. Releasing changes

GitOps watches `main`. Feature work lands on a branch (this work is on
`claude/charming-hopper-7dwif7`); **merging that branch into `main`** triggers
Portainer to rebuild and redeploy. CI (`.github/workflows/ci.yml`) gates merges.

## 7. Backups & restore

The `db-backup` sidecar dumps the DB daily (retention via `BACKUP_KEEP_*`). By
default dumps land in `./backups` beside the stack — **set `BACKUP_PATH` to an
absolute host path on a different disk or a LAN file-server mount** so losing the
host disk doesn't take `pgdata` *and* its only backups. A quick off-host copy,
e.g. a cron `rsync $BACKUP_PATH fileserver:/procurement-backups/`.

**Restore** (tested procedure):
1. Stop the app so nothing writes mid-restore: in Portainer, stop the
   `procurement` app service (leave `db` running).
2. Pick a dump from `$BACKUP_PATH` (they're gzipped `pg_dump`, newest =
   `last/fmp-latest.sql.gz`).
3. Restore into the running db container:
   `zcat fmp-latest.sql.gz | docker exec -i <db-container> psql -U fmp -d fmp`
   (drop/recreate the `fmp` DB first if doing a clean restore).
4. Start the app; it runs `alembic upgrade head` on boot, so a dump from an
   older schema is migrated forward automatically. Verify at `/health` (db: ok)
   and spot-check the Stock + Order Page.

Do NOT rotate `DB_PASSWORD` — it is permanent for the life of `pgdata`.

## 8. Guardrails (do not break — these caused real outages)

- Keep `traefik.docker.network=web`. No `ports:` on app/db. FastAPI stays plain
  HTTP on `8000` (Traefik does TLS) — don't add `loadbalancer.server.scheme=https`.
- `DB_PASSWORD` is permanent once `pgdata` exists. Secrets only in Portainer.
- Hostnames/labels use `gml.com.fj`; the mail identity `no-reply@golden.com.fj`
  is a different domain on purpose.
