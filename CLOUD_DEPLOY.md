# Deploying Golden Procurement (Cloud setup)

This is the **cloud** deployment: a hybrid that keeps the integration-facing
backend on the LAN while moving the UI, database, and auth to managed cloud
services. It sits alongside the on-prem `DEPLOY.md` (Portainer/Traefik) — either
can run; this doc is the cloud path.

```
   Browser
     │  https://procurement.gml.com.fj  (or *.vercel.app)
     ▼
 ┌─────────────┐   Clerk session JWT (Bearer)   ┌──────────────────────────┐
 │   Vercel    │ ───────────────────────────►   │  Azure Linux VM          │
 │  React SPA  │   /api, /auth  (Vercel rewrite)│  FastAPI backend         │
 │  + Clerk JS │ ◄───────────────────────────   │  mcp.golden.com.fj :8000 │
 └─────────────┘                                 │  + schedulers            │
        │ sign-in                                └───────┬──────────┬───────┘
        ▼                                                │ VPN      │ TLS
 ┌─────────────┐                                 ┌───────▼───┐  ┌───▼─────────┐
 │    Clerk    │  (Microsoft/Entra federated)    │  LAN:     │  │  Supabase   │
 │  identity   │                                 │  BC /     │  │  Postgres   │
 └─────────────┘                                 │  Kiwiplan │  │ (procurement)│
                                                 │  / Accura │  └─────────────┘
                                                 └───────────┘
```

**Why hybrid:** the backend runs the always-on schedulers (stock refresh, outbox
PO posting, usage import) and talks to BC (OData/NTLM), Kiwiplan and Accura
(ODBC) over the LAN — none of that fits a serverless host. The Azure VM is
already on the VPN to the LAN and published at `mcp.golden.com.fj`, so it is the
backend's home. The gateway still lives in-app (CLAUDE.md §2); nothing about the
"app owns canonical state" rule changes.

Components:
| Piece | Where | Notes |
|-------|-------|-------|
| Frontend (React/Vite) | **Vercel** | `frontend/`, `frontend/vercel.json` |
| Auth | **Clerk** | Microsoft/Entra federated; roles → local role codes |
| Database | **Supabase** | project `procurement`, region ap-southeast-2 |
| Backend (FastAPI + gateway) | **Azure VM** | `docker-compose.cloud.yml`, `mcp.golden.com.fj` |

---

## 1. Supabase (database)

The `procurement` project already exists (org `sameer@golden.com.fj's Org`,
region **ap-southeast-2**, ref **`pwwycyyvtwgfqjgoykis`**).

1. Supabase dashboard → **Project → Settings → Database → Connection string**.
2. Use the **Session pooler** (host `aws-0-ap-southeast-2.pooler.supabase.com`,
   **port 5432**) — this app keeps a long-lived SQLAlchemy pool + prepared
   statements, so do **not** use the transaction pooler (6543).
3. Build `DATABASE_URL` for the backend (note the `postgresql+psycopg` driver and
   `sslmode=require`):
   ```
   postgresql+psycopg://postgres.pwwycyyvtwgfqjgoykis:<DB_PASSWORD>@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   `<DB_PASSWORD>` is the database password from **Settings → Database** (reset it
   there if you don't have it).
4. **Schema:** nothing to run by hand. The backend runs `alembic upgrade head` on
   startup (Alembic is the single source of truth), seeds the five roles + the
   bootstrap admin, and — on an empty DB with no integrations — loads a small demo
   catalog. Backups are managed by Supabase (no `db-backup` sidecar in the cloud
   compose).

## 2. Clerk (auth)

Clerk is the sign-in layer, with Microsoft/Entra federated so staff keep using
their Golden accounts.

1. **Create the application** in the Clerk dashboard (production instance for
   `golden.com.fj`).
2. **Enable Microsoft SSO:** *User & Authentication → SSO connections → Microsoft*
   (or an *Enterprise SSO / SAML* connection to your Entra tenant). Add the
   redirect/callback URLs Clerk shows you into the Entra app registration.
3. **Roles → the app's 5 role codes.** The backend maps a `role` claim to
   `REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN` using the **same exact-match
   rule as Entra** (never substring — `Finance-Admins` can't become `ADMIN`).
   Two supported ways to carry the role:
   - **publicMetadata** on the user: `{ "role": "OFFICER" }`, surfaced via a JWT
     template (below); or
   - **Clerk Organizations** roles, surfaced as `org_role`.
   Unmapped users fall back to `VIEWER` (`DEFAULT_ROLE`).
4. **JWT template (recommended)** so the session token carries what the backend
   needs. *Configure → JWT templates → New* named e.g. `procurement`:
   ```json
   {
     "email": "{{user.primary_email_address}}",
     "name": "{{user.full_name}}",
     "role": "{{user.public_metadata.role}}"
   }
   ```
   Set `VITE_CLERK_JWT_TEMPLATE=procurement` on Vercel so the frontend requests
   this template. *(Alternative: skip the template and set `CLERK_SECRET_KEY` on
   the backend — it will look up email/name/role from the Clerk API instead.)*
5. **Keys:** copy the **Publishable key** (`pk_live_…`, → Vercel), the **Secret
   key** (`sk_live_…`, → backend, optional), and the **Issuer** URL
   (*API keys → Show JWT public key / Issuer*, e.g. `https://clerk.golden.com.fj`
   → backend `CLERK_ISSUER`).

## 3. Backend (Azure Linux VM)

Deploy `docker-compose.cloud.yml` on the VM (via Portainer or `docker compose`).
It builds one image (`procurement-cloud-app`), runs the API on `:8000`, and is
published at `mcp.golden.com.fj` by the VM's Traefik. (If the VM uses a different
reverse proxy, drop the Traefik labels, uncomment `ports: 127.0.0.1:8000:8000`,
and point that proxy at it — see the comments in the compose.)

**Required env** (set on the VM / Portainer — never commit):

| Var | Value |
|-----|-------|
| `APP_HOST` | `mcp.golden.com.fj` |
| `DATABASE_URL` | the Supabase session-pooler URL from §1 |
| `SECRET_KEY` | 32+ random chars (signs the break-glass cookie) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | break-glass admin |
| `CLERK_ISSUER` | from §2.5 |
| `CLERK_AUTHORIZED_PARTIES` | your Vercel origin(s), comma-separated |
| `CORS_ORIGINS` | Vercel origin(s) — **only if not** using Vercel rewrites |
| `CLERK_SECRET_KEY` | optional (API user lookup) |

Plus the integration vars (`BC_*`, `KIWIPLAN_*`, `ACCURA_*`, `GRAPH_*`,
`AZURE_SQL_DSN`) exactly as on-prem — they resolve over the VPN. Anything unset
stays in clearly-flagged demo mode.

Verify: `curl https://mcp.golden.com.fj/health` → `{"status":"ok","db":"ok"}`.

## 4. Frontend (Vercel)

1. **Import** this repo into the Vercel team *Sameer Mohammed's projects*.
2. **Root directory:** `frontend`. Framework auto-detects as **Vite** (settings
   also come from `frontend/vercel.json`).
3. **Environment variables:**
   | Var | Value |
   |-----|-------|
   | `VITE_CLERK_PUBLISHABLE_KEY` | `pk_live_…` from Clerk |
   | `VITE_CLERK_JWT_TEMPLATE` | `procurement` (if you made the template) |
   | `VITE_API_BASE_URL` | **leave blank** — `vercel.json` rewrites `/api`+`/auth` to `mcp.golden.com.fj` (same-origin, no CORS). Set it to `https://mcp.golden.com.fj` only if you prefer direct cross-origin calls (then also set `CORS_ORIGINS` on the backend). |
4. **Deploy.** Add your custom domain (e.g. `procurement.gml.com.fj`) in
   *Vercel → Domains* and point DNS at Vercel when ready to cut over.

> `vercel.json` proxies `/api`, `/auth`, `/health` to `mcp.golden.com.fj` and
> serves the SPA for everything else. Keeping the browser same-origin means no
> CORS config and the break-glass admin cookie keeps working through Vercel.

## 5. Bring-up order & verification

1. Supabase reachable (`DATABASE_URL` correct).
2. Backend up on the VM → `/health` green (proves DB connectivity + migrations ran).
3. Clerk app live with Microsoft connection + keys.
4. Vercel deployed with the Clerk publishable key.
5. Open the Vercel URL → **Sign in** (Clerk shows Microsoft) → land on the
   Dashboard. Confirm your role pill (top-right) matches your Clerk role.
6. Break-glass: hitting `https://mcp.golden.com.fj` directly serves the fallback
   admin login (`ADMIN_USERNAME`/`ADMIN_PASSWORD`) — for emergencies when Clerk
   is unavailable.

## 6. How auth resolves (reference)

`get_current_user` (backend) tries, in order:
1. **`Authorization: Bearer <clerk_jwt>`** — verified against Clerk's JWKS
   (RS256, issuer + expiry + authorized-party checked), role claim mapped, local
   `User` upserted by `clerk_user_id` (email fallback). This is the normal path.
2. **Signed session cookie** — break-glass admin login and legacy Entra OIDC.

The app remains the only writer of canonical state; Clerk asserts identity +
entitlement only. RBAC (`role_code`, `approval_limit`, OFFICER/ADMIN-only
mutations) is unchanged from on-prem.

## 7. Notes / guardrails

- **Session pooler, not transaction pooler** for `DATABASE_URL` (long-lived pool).
- **Unique image name** (`procurement-cloud-app`) — never share an image name
  between stacks (on-prem lesson: shared names cause redeploy churn).
- The `mcp.golden.com.fj` backend must stay reachable from Vercel (public) **and**
  from the LAN over the VPN (integrations). Don't firewall it to LAN-only.
- Secrets live in the VM/Portainer, Vercel, Clerk, and Supabase dashboards only —
  `.env.example` is documentation.
- On-prem `DEPLOY.md` / `docker-compose.traefik.yml` remain valid if you ever fall
  back to the fully on-prem deployment.
