"""Server test configuration — shared async engine with StaticPool."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import ApiKey, Organization

# Single in-memory engine shared for the full test session
_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = async_sessionmaker(
    bind=_engine, class_=AsyncSession, expire_on_commit=False
)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(scope="session", autouse=True)
async def create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with _TestSession() as session:
        yield session


@pytest.fixture
async def org_and_key(db) -> tuple[Organization, str]:
    """Create a unique org + API key for each test."""
    org = Organization(id=str(uuid.uuid4()), name="Test Org")
    db.add(org)

    raw_key = f"akt_live_{uuid.uuid4().hex}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        org_id=org.id,
        name="test key",
        key_prefix=raw_key[:12],
        key_hash=key_hash,
    )
    db.add(api_key)
    await db.commit()
    return org, raw_key


@pytest.fixture
async def client(org_and_key) -> AsyncClient:
    _, raw_key = org_and_key
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw_key}"},
    ) as c:
        yield c
