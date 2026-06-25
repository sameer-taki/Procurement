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


async def _scheduler() -> None:
    """Periodic stock refresh (~every settings.stock_refresh_seconds)."""
    while True:
        await asyncio.sleep(settings.stock_refresh_seconds)
        try:
            await asyncio.to_thread(_refresh_job)
            log.info("stock refresh complete")
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled stock refresh failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_secret()
    if settings.run_migrations_on_startup:
        run_migrations()
    with Session(engine) as s:
        seed_roles_and_admin(s)
        if settings.seed_demo_on_empty and s.exec(select(Item)).first() is None:
            # First boot with unconfigured systems → populate from demo data so the
            # Stock view is immediately usable. No-op once real items exist.
            stock_service.refresh_all(s)
            log.info("seeded initial catalog + stock")

    task = asyncio.create_task(_scheduler()) if settings.stock_refresh_enabled else None
    try:
        yield
    finally:
        if task:
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
    return {"status": "ok", "env": settings.app_env}


# Auth + API routers (all API endpoints under /api). Imported after app exists so
# their module-level dependencies resolve cleanly.
from .auth.routes import router as auth_router          # noqa: E402
from .auth.routes import me_router                        # noqa: E402
from .domain import requisitions as requisition_routes    # noqa: E402

app.include_router(auth_router)
app.include_router(me_router)
app.include_router(stock_routes.router)
app.include_router(requisition_routes.router)


# Serve the built React UI (present in the image at app/static), with SPA fallback
# so client-side routes deep-link correctly.
_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    _assets = os.path.join(_static, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith(("api/", "auth/", "health")):
            raise HTTPException(status_code=404)
        candidate = os.path.join(_static, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_static, "index.html"))
