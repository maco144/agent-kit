"""
agentkit-server — org and API key administration for a self-hosted agent-kit Cloud.

    agentkit-server create-org "Rising Sun"
    agentkit-server create-key <org-id> --name aios
    agentkit-server list-orgs | list-keys [--org <org-id>] | revoke-key <key-id>

Reads DATABASE_URL from the environment, like the server itself. A created key is printed once and stored
only as a SHA-256 hash.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
from collections.abc import Sequence

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ApiKey, Organization

KEY_PREFIX_LEN = 13  # "akt_live_" + 4 hex characters, as stored by app/auth.py


async def create_org(name: str, tier: str = "free") -> int:
    async with SessionLocal() as db:
        org = Organization(name=name, tier=tier)
        db.add(org)
        await db.commit()
        print(f"created org {name!r} ({tier}) with id {org.id}")
    return 0


async def list_orgs() -> int:
    async with SessionLocal() as db:
        rows = (await db.execute(select(Organization).order_by(Organization.created_at))).scalars().all()
    if not rows:
        print("no organizations")
        return 0
    for org in rows:
        print(f"{org.id}  {org.tier:<10} {org.name}")
    return 0


async def create_key(org_id: str, name: str = "default") -> int:
    async with SessionLocal() as db:
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one_or_none()
        if org is None:
            print(f"org {org_id!r} not found")
            return 2
        raw_key = "akt_live_" + secrets.token_hex(24)
        db.add(ApiKey(
            org_id=org.id,
            name=name,
            key_prefix=raw_key[:KEY_PREFIX_LEN],
            key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
        ))
        await db.commit()
    print("store this key now - it cannot be shown again:")
    print(f"  {raw_key}")
    return 0


async def list_keys(org_id: str | None = None) -> int:
    async with SessionLocal() as db:
        query = select(ApiKey).order_by(ApiKey.created_at)
        if org_id:
            query = query.where(ApiKey.org_id == org_id)
        rows = (await db.execute(query)).scalars().all()
    if not rows:
        print("no api keys")
        return 0
    for key in rows:
        used = key.last_used_at.isoformat(timespec="seconds") if key.last_used_at else "never"
        print(f"{key.id}  {key.key_prefix}...  {key.name:<20} org={key.org_id}  last used {used}")
    return 0


async def revoke_key(key_id: str) -> int:
    async with SessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
        if key is None:
            print(f"api key {key_id!r} not found")
            return 2
        await db.delete(key)
        await db.commit()
    print(f"revoked api key {key_id}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentkit-server", description="agent-kit Cloud administration")
    sub = parser.add_subparsers(dest="command", required=True)

    org = sub.add_parser("create-org", help="create an organization")
    org.add_argument("name")
    org.add_argument("--tier", default="free", choices=["free", "pro", "enterprise"])

    sub.add_parser("list-orgs", help="list organizations")

    key = sub.add_parser("create-key", help="issue an API key for an organization")
    key.add_argument("org_id")
    key.add_argument("--name", default="default", help="label shown by list-keys")

    keys = sub.add_parser("list-keys", help="list API keys (prefixes only)")
    keys.add_argument("--org", dest="org_id", default=None)

    revoke = sub.add_parser("revoke-key", help="delete an API key")
    revoke.add_argument("key_id")
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if os.environ.get("DATABASE_URL", "").startswith("sqlite"):
        from app.database import init_db  # dev convenience, as in app/main.py

        await init_db()
    if args.command == "create-org":
        return await create_org(args.name, args.tier)
    if args.command == "list-orgs":
        return await list_orgs()
    if args.command == "create-key":
        return await create_key(args.org_id, args.name)
    if args.command == "list-keys":
        return await list_keys(args.org_id)
    return await revoke_key(args.key_id)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_dispatch(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
