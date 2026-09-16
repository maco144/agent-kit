"""agentkit-server: org and API key administration."""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import cli
from app.main import app
from app.models import ApiKey, Organization


class _Session:
    """Hands a CLI command the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _cli_session(monkeypatch, db):
    monkeypatch.setattr(cli, "SessionLocal", lambda: _Session(db))


def printed(capsys) -> str:
    return capsys.readouterr().out


async def test_create_org_and_key_then_authenticate(capsys, db):
    assert await cli.create_org("Rising Sun", "free") == 0
    org_id = printed(capsys).split()[-1]

    assert await cli.create_key(org_id, "aios") == 0
    out = printed(capsys)
    key = [word for word in out.split() if word.startswith("akt_live_")][0]
    assert len(key) == len("akt_live_") + 48
    assert out.count(key) == 1  # printed exactly once

    stored = (await db.execute(select(ApiKey).where(ApiKey.org_id == org_id))).scalar_one()
    assert stored.key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert stored.key_prefix == key[:13]
    assert stored.name == "aios"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {key}"}) as client:
        assert (await client.get("/v1/metrics/summary")).status_code == 200


async def test_list_orgs_and_keys_show_rows_without_secrets(capsys, db):
    await cli.create_org("Listed Org", "pro")
    org_id = printed(capsys).split()[-1]
    await cli.create_key(org_id, "first")
    key = [word for word in printed(capsys).split() if word.startswith("akt_live_")][0]

    assert await cli.list_orgs() == 0
    orgs_out = printed(capsys)
    assert "Listed Org" in orgs_out and org_id in orgs_out and "pro" in orgs_out

    assert await cli.list_keys(org_id) == 0
    keys_out = printed(capsys)
    assert "first" in keys_out and key[:13] in keys_out
    assert key not in keys_out  # only the prefix, never the key
    assert "never" in keys_out  # last used


async def test_revoke_key_stops_authentication(capsys, db):
    await cli.create_org("Revoked Org", "free")
    org_id = printed(capsys).split()[-1]
    await cli.create_key(org_id, "temp")
    key = [word for word in printed(capsys).split() if word.startswith("akt_live_")][0]
    key_id = (await db.execute(select(ApiKey).where(ApiKey.org_id == org_id))).scalar_one().id

    assert await cli.revoke_key(key_id) == 0
    assert "revoked" in printed(capsys).lower()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {key}"}) as client:
        assert (await client.get("/v1/metrics/summary")).status_code == 401


async def test_unknown_ids_exit_non_zero(capsys):
    assert await cli.create_key("no-such-org", "x") == 2
    assert "not found" in printed(capsys).lower()
    assert await cli.revoke_key("no-such-key") == 2
    assert "not found" in printed(capsys).lower()


async def test_create_org_records_the_tier(capsys, db):
    await cli.create_org("Tiered Org", "enterprise")
    org_id = printed(capsys).split()[-1]
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
    assert (org.name, org.tier) == ("Tiered Org", "enterprise")


def test_main_runs_as_a_command(tmp_path):
    """main() wires argparse to the commands and runs its own event loop."""
    env = {"DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}", "PATH": "/usr/bin:/bin"}
    made = subprocess.run([sys.executable, "-m", "app.cli", "create-org", "CLI Org"],
                          capture_output=True, text=True, env=env)
    assert made.returncode == 0, made.stderr
    org_id = made.stdout.split()[-1]

    listed = subprocess.run([sys.executable, "-m", "app.cli", "list-orgs"],
                            capture_output=True, text=True, env=env)
    assert listed.returncode == 0 and org_id in listed.stdout

    missing = subprocess.run([sys.executable, "-m", "app.cli", "revoke-key", "nope"],
                             capture_output=True, text=True, env=env)
    assert missing.returncode == 2
