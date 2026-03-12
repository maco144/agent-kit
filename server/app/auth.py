"""API key authentication dependency."""

from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ApiKey, Organization

_bearer = HTTPBearer(auto_error=True)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_current_org(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    """
    FastAPI dependency — resolves an API key to an Organization.

    Raises HTTP 401 if the key is missing, malformed, or not found.
    """
    raw_key = credentials.credentials
    if not raw_key.startswith("akt_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format.",
        )

    key_hash = _hash_key(raw_key)
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash)
    )
    api_key = result.scalar_one_or_none()

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key not found.",
        )

    # Touch last_used_at (best-effort, don't fail the request if this errors)
    try:
        from datetime import datetime
        api_key.last_used_at = datetime.utcnow()
        await db.commit()
    except Exception:
        await db.rollback()

    org_result = await db.execute(
        select(Organization).where(Organization.id == api_key.org_id)
    )
    org = org_result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization not found.",
        )

    return org
