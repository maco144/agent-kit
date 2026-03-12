"""Alembic environment configuration — async SQLAlchemy."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can detect them for autogenerate
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.database import Base  # noqa: E402
import app.models  # noqa: E402, F401 — registers all tables with Base.metadata

target_metadata = Base.metadata

DATABASE_URL = os.environ.get(
    "DATABASE_URL", config.get_main_option("sqlalchemy.url", "")
)


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
