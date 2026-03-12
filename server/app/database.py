"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Defaults to SQLite in-memory for local dev; override with DATABASE_URL env var.
# Production: postgresql+asyncpg://user:pass@host/dbname
# Local dev:  sqlite+aiosqlite:///./agentkit_cloud.db
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite+aiosqlite:///./agentkit_cloud.db"
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    # PostgreSQL-specific pool settings are ignored by SQLite
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a database session per request."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables. Used in tests and local dev; production uses Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
