import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import engine, run_migrations, seed_roles_and_admin
from .domain import stock as stock_routes
from .domain import stock_service
from .gateway.models import Item

log = logging.getLogger("golden.procurement")

PLACEHOLDER_SECRET = "CHANGE_ME_32_CHARS_MINIMUM_PLACEHOLDER"


def _check_secret() -> None:
    if settings.is_production and (
        settings.secret_key == PLACEHOLDER_SECRET or len(settings.secret_key) < 32
    ):
        raise RuntimeError("SECRET_KEY must be set to a strong 32+ char value in production")


def _refresh_job() -> None:
    with Session(engine) as s:
        stock_service.refresh_all(s)


def _outbox_job() -> None:
    from .domain import purchasing
    with Session(engine) as s:
        purchasing.process_outbox(s)


def _usage_import_job() -> None:
    from .domain import planning
    with Session(engine) as s:
        planning.import_usage(s)


async def _scheduler() -> None:
    """Periodic stock refresh (~every settings.stock_refresh_seconds)."""
    while True:
        await asyncio.sleep(settings.stock_refresh_seconds)
        try:
            await asyncio.to_thread(_refresh_job)
            log.info("stock refresh complete")
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled stock refresh failed")


async def _outbox_scheduler() -> None:
    """Periodically drain the integration outbox (retries failed BC posts).
    Idempotent: an already-posted PO is skipped via its ExternalRef, never reposted."""
    while True:
        await asyncio.sleep(settings.outbox_process_seconds)
        try:
            await asyncio.to_thread(_outbox_job)
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled outbox processing failed")


async def _usage_import_scheduler() -> None:
    """Periodic BC usage import (SOP §9 cadence) so the planning run's trailing
    averages stay current without a manual import. Idempotent upsert; a failed
    run (BC down, concurrent manual import) just waits for the next tick."""
    while True:
        await asyncio.sleep(settings.usage_import_seconds)
        try:
            await asyncio.to_thread(_usage_import_job)
            log.info("scheduled usage import complete")
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled usage import failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_secret()
    if settings.run_migrations_on_startup:
        run_migrations()
    with Session(engine) as s:
        seed_roles_and_admin(s)
        if settings.seed_demo_on_empty and s.exec(select(Item)).first() is None:
            # First boot → populate so the Stock view is usable. GUARDED: a live
            # BC that is slow or down at startup must not block or crash the boot
            # (that would loop-restart the container); the 30-min scheduler will
            # populate on its next tick. No-op once real items exist.
            try:
                stock_service.refresh_all(s)
                log.info("seeded initial catalog + stock")
            except Exception:
                log.exception("initial seed failed; scheduler will retry")

    tasks = []
    if settings.stock_refresh_enabled:
        tasks.append(asyncio.create_task(_scheduler()))
    if settings.outbox_process_enabled:
        tasks.append(asyncio.create_task(_outbox_scheduler()))
    if settings.usage_import_enabled:
        tasks.append(asyncio.create_task(_usage_import_scheduler()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()


app = FastAPI(title="Golden Procurement", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    max_age=settings.session_max_age,
    https_only=settings.cookie_secure,
    same_site="lax",
)


@app.get("/health")
def health():
    """Liveness + DB readiness. Returns 200 with db='ok' when the database
    answers SELECT 1, else 503 with db='error' so a monitor / the compose
    healthcheck sees DB-down instead of a falsely-green app."""
    from sqlalchemy import text
    from fastapi.responses import JSONResponse
    try:
        with Session(engine) as s:
            s.execute(text("SELECT 1"))
        db = "ok"
    except Exception:
        log.exception("health check: DB probe failed")
        db = "error"
    body = {"status": "ok" if db == "ok" else "degraded", "env": settings.app_env, "db": db}
    return JSONResponse(body, status_code=200 if db == "ok" else 503)


# Auth + API routers (all API endpoints under /api). Imported after app exists so
# their module-level dependencies resolve cleanly.
from .auth.routes import router as auth_router          # noqa: E402
from .auth.routes import me_router                        # noqa: E402
from .domain import requisitions as requisition_routes    # noqa: E402
from .domain import purchasing as purchasing_routes        # noqa: E402
from .domain import bom_service as bom_routes               # noqa: E402
from .domain import analytics as analytics_routes           # noqa: E402
from .domain import planning as planning_routes             # noqa: E402
from .domain import forecasts as forecast_routes            # noqa: E402
from .domain import shipments as shipment_routes            # noqa: E402
from .domain import admin as admin_routes                    # noqa: E402
from .domain import reports as report_routes                 # noqa: E402

app.include_router(auth_router)
app.include_router(me_router)
app.include_router(stock_routes.router)
app.include_router(requisition_routes.router)
app.include_router(purchasing_routes.router)
app.include_router(bom_routes.router)
app.include_router(analytics_routes.router)
app.include_router(planning_routes.router)
app.include_router(forecast_routes.router)
app.include_router(shipment_routes.router)
app.include_router(admin_routes.router)
app.include_router(report_routes.router)


# Serve the built React UI (present in the image at app/static), with SPA fallback
# so client-side routes deep-link correctly.
_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    _assets = os.path.join(_static, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    _index = os.path.join(_static, "index.html")
    _static_root = os.path.realpath(_static)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith(("api/", "auth/", "health")):
            raise HTTPException(status_code=404)
        # Serve a real static file ONLY when the resolved path stays inside the
        # build dir. os.path.join + FileResponse alone is a path-traversal hole:
        # uvicorn percent-decodes '..%2f' into '../' AFTER routing, so a crafted
        # path could escape _static and read arbitrary files. realpath-contain
        # every candidate; anything outside (or not a real file) falls back to
        # the SPA shell so client-side routes still deep-link.
        if full_path:
            candidate = os.path.realpath(os.path.join(_static, full_path))
            if (
                (candidate == _static_root or candidate.startswith(_static_root + os.sep))
                and os.path.isfile(candidate)
            ):
                return FileResponse(candidate)
        return FileResponse(_index)
