"""agent-kit Cloud — FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routers import alerts, audit, ingest, metrics


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import asyncio
    import logging
    import os

    _log = logging.getLogger("agentkit.cloud")

    # On startup: ensure tables exist (dev/test mode; production uses Alembic)
    if os.environ.get("DATABASE_URL", "").startswith("sqlite"):
        await init_db()

    # Start background alert evaluation worker (opt-in via env var)
    worker_task = None
    if os.environ.get("ENABLE_ALERT_WORKER", "").lower() in ("1", "true"):
        async def _alert_worker() -> None:
            from app.alerting.evaluator import evaluate_all_rules
            from app.database import SessionLocal
            while True:
                await asyncio.sleep(60)
                try:
                    async with SessionLocal() as db:
                        await evaluate_all_rules(db)
                        await db.commit()
                except Exception as exc:
                    _log.warning("Alert worker error: %s", exc)

        worker_task = asyncio.create_task(_alert_worker(), name="agentkit-alert-worker")
        _log.info("Alert evaluation worker started (60s cadence)")

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


@app.get("/healthz", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
