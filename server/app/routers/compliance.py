"""Compliance exports: signing keys, evidence bundles, retention, legal holds, deletion receipts."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import signing
from app.database import get_db

wellknown_router = APIRouter(tags=["compliance"])
router = APIRouter(prefix="/v1/compliance", tags=["compliance"])


@wellknown_router.get(signing.KEYS_PATH)
async def signing_keys(db: AsyncSession = Depends(get_db)) -> dict[str, list[dict[str, object]]]:
    """Public keys for verifying evidence bundles and deletion receipts. No authentication."""
    await signing.active_key(db)  # ensure a key exists before anyone needs to verify
    await db.commit()
    return {"keys": await signing.public_keys(db)}
