"""agent-kit Cloud — FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routers import alerts, audit, budgets, compliance, ingest, metrics, otlp, support


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import asyncio
    import logging
    import os

    _log = logging.getLogger("agentkit.cloud")

    # On startup: ensure tables exist (dev/test mode; production uses Alembic)
    if os.environ.get("DATABASE_URL", "").startswith("sqlite"):
        await init_db()

    # The alert worker normally runs as its own container (python -m app.worker).
    # ENABLE_ALERT_WORKER=1 runs it in-process instead, for local development.
    worker_task = None
    if os.environ.get("ENABLE_ALERT_WORKER", "").lower() in ("1", "true"):
        from app.worker import run_forever

        worker_task = asyncio.create_task(run_forever(), name="agentkit-alert-worker")
        _log.info("Alert evaluation worker started in-process (60s cadence)")

    yield

    # On shutdown: cancel background worker if running
    if worker_task and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="agent-kit Cloud",
    description="Ingest API and audit trail service for agent-kit.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(audit.router)
app.include_router(metrics.router)
app.include_router(alerts.router)
app.include_router(support.router)
app.include_router(otlp.router)
app.include_router(budgets.router)
app.include_router(compliance.wellknown_router)
app.include_router(compliance.router)


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """Public service pointer — the API itself lives under /v1 and needs an API key."""
    return {
        "service": "agent-kit Cloud",
        "version": app.version,
        "docs": "/docs",
        "health": "/healthz",
        "source": "https://github.com/maco144/agent-kit",
    }


@app.get("/healthz", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
