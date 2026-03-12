"""agent-kit Cloud — FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routers import audit, ingest


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # On startup: ensure tables exist (dev/test mode; production uses Alembic)
    import os
    if os.environ.get("DATABASE_URL", "").startswith("sqlite"):
        await init_db()
    yield
    # On shutdown: nothing needed — SQLAlchemy pools close themselves


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


@app.get("/healthz", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
