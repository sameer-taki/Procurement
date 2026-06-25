import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # TODO (Phase 1): run Alembic migrations and seed the first admin + roles.
    # For early local dev only, you may create_all here; switch to Alembic before P2.
    yield


app = FastAPI(title="Golden Procurement", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}


# API routers — Claude Code adds these per phase, all under /api
# from .domain.requisitions import router as requisitions_router
# app.include_router(requisitions_router, prefix="/api")

# Serve the built React UI (present in the image at app/static)
_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
