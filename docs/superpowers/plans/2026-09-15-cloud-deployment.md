# Cloud Deployment (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up -d --build` in `server/` runs agent-kit Cloud (Postgres, API, one alert worker), `agentkit-server` creates orgs and API keys, and the stack serves `https://agentkit.eudaimonia.win` from rising with AIOS agents reporting to it over localhost.

**Architecture:** The alert loop moves out of the FastAPI lifespan into `app/worker.py`, run by its own container so exactly one process evaluates alerts. A new `app/cli.py` (console script `agentkit-server`) does org and key admin against the same async session factory. A Dockerfile builds one image used by both the API and the worker; the entrypoint applies Alembic migrations before starting either. The API publishes only on `127.0.0.1`; the existing Caddy on rising terminates TLS.

**Tech Stack:** Docker + Compose v2, `postgres:16-alpine`, `python:3.12-slim`, FastAPI/uvicorn, SQLAlchemy async, Alembic, argparse, pytest-asyncio, Caddy (already installed on rising).

**Spec:** `specs/18-cloud-deployment.md`

## Global Constraints

- The API never publishes on a public interface: `ports: ["127.0.0.1:${API_PORT:-8020}:8000"]`. Caddy is the only public entry point.
- Exactly one alert worker: the `api` service sets `ENABLE_ALERT_WORKER=0`; the `worker` service runs `python -m app.worker`.
- One implementation of the evaluation loop, in `app/worker.py`, used by both the worker process and `main.py`'s opt-in in-process mode.
- Migrations run in the entrypoint (`alembic upgrade head`) and only for the API container: the worker sets `RUN_MIGRATIONS=0`.
- API keys are `"akt_live_" + secrets.token_hex(24)`; store `key_prefix` (first 13 characters) and the SHA-256 hash only, matching `server/app/auth.py`. Print the key exactly once.
- Secrets live in `server/.env` (git-ignored via the repo's `.env` rule) and never in the image, the repo, or a shell history file. `AGENTKIT_SIGNING_KEY` is a base64-encoded 32-byte seed.
- rising facts: user `linuxuser` (in the `docker` group, passwordless sudo), Docker Compose v5.1.0, Caddy active on 80/443 with 31 sites in `/etc/caddy/Caddyfile`, public IP 45.77.104.159, host ports already taken include 8000, 8001, 8010–8012, 8040, 8101, 3002, 3003, 4173, 222 — agent-kit uses **8020**.
- There is no Docker on the development machine: the image, compose file, and entrypoint are verified on rising in Task 5, not locally.
- Gates for Tasks 1–4: `cd server && python3 -m pytest -q`, `ruff check app tests`, plus the SDK gates (`python3 -m pytest -q`, `.venv/bin/python -m pytest -q`, `ruff check agent_kit tests`, `.venv/bin/python -m mypy agent_kit`) when anything outside `server/` changes.

---

### Task 1: Extract the alert loop into `app/worker.py`

**Files:**
- Create: `server/app/worker.py`
- Modify: `server/app/main.py` (lifespan)
- Test: `server/tests/test_worker.py` (create)

**Interfaces:**
- Produces: `app.worker.CYCLE_SECONDS = 60.0`; `async run_cycle() -> None`; `async run_forever(cycle_seconds: float = CYCLE_SECONDS) -> None`; `main() -> None` (module entry point for `python -m app.worker`).

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_worker.py`:

```python
"""The background evaluation loop: one implementation, used by the worker container and main.py."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import worker


@pytest.fixture(autouse=True)
def _session_factory(monkeypatch, db):
    """Point the worker at the test database (conftest's session factory)."""
    monkeypatch.setattr(worker, "SessionLocal", lambda: _Session(db))


class _Session:
    """Hands the worker the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_run_cycle_evaluates_rules_budgets_and_retention(monkeypatch):
    called: list[str] = []

    def recorder(name):
        async def inner(db):
            called.append(name)
        return inner

    monkeypatch.setattr("app.alerting.evaluator.evaluate_all_rules", recorder("rules"))
    monkeypatch.setattr("app.budgets.evaluate_all_budgets", recorder("budgets"))
    monkeypatch.setattr("app.compliance.retention.purge_expired", recorder("retention"))

    await worker.run_cycle()

    assert called == ["rules", "budgets", "retention"]


async def test_run_forever_logs_errors_and_keeps_going(monkeypatch, caplog):
    attempts: list[int] = []

    async def boom(db):
        attempts.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr("app.alerting.evaluator.evaluate_all_rules", boom)

    with caplog.at_level(logging.WARNING, logger="agentkit.cloud.worker"):
        task = asyncio.create_task(worker.run_forever(cycle_seconds=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(attempts) >= 2  # the loop survived the first failure
    assert any("db down" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -m pytest tests/test_worker.py -q`
Expected: collection error — `ImportError: cannot import name 'worker' from 'app'`.

- [ ] **Step 3: Implement `server/app/worker.py`**

```python
"""
Background evaluation loop: alert rules, fleet budgets, retention purges.

Runs as its own container (``python -m app.worker``) so exactly one process evaluates alerts, however many
API workers are serving traffic. ``main.py`` can also run it in-process with ENABLE_ALERT_WORKER=1 for local
development.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.database import SessionLocal

logger = logging.getLogger("agentkit.cloud.worker")

CYCLE_SECONDS = 60.0


async def run_cycle() -> None:
    """One pass: evaluate alert rules, budgets, and retention, then commit."""
    from app.alerting.evaluator import evaluate_all_rules
    from app.budgets import evaluate_all_budgets
    from app.compliance.retention import purge_expired

    async with SessionLocal() as db:
        await evaluate_all_rules(db)
        await evaluate_all_budgets(db)
        await purge_expired(db)
        await db.commit()


async def run_forever(cycle_seconds: float = CYCLE_SECONDS) -> None:
    """Run a cycle every ``cycle_seconds``. A failed cycle is logged; the loop continues."""
    while True:
        await asyncio.sleep(cycle_seconds)
        try:
            await run_cycle()
            logger.debug("alert worker cycle complete")
        except Exception as exc:
            logger.warning("Alert worker error: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("agent-kit Cloud worker started (%.0fs cadence)", CYCLE_SECONDS)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Rewrite the lifespan in `server/app/main.py`**

Replace the whole `lifespan` function with:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && python3 -m pytest tests/test_worker.py -q` → PASS (2 tests). Then `cd server && python3 -m pytest -q` (166 existing tests plus these) and `cd server && ruff check app tests`.

- [ ] **Step 6: Commit**

```bash
git add server/app/worker.py server/app/main.py server/tests/test_worker.py
git commit -m "feat(server): run the alert loop as its own process"
```

---

### Task 2: `agentkit-server` admin CLI

**Files:**
- Create: `server/app/cli.py`
- Modify: `server/pyproject.toml` (`[project.scripts]`)
- Test: `server/tests/test_cli.py` (create)

**Interfaces:**
- Consumes: `app.models.Organization`, `app.models.ApiKey`, `app.database.SessionLocal`.
- Produces: async commands `app.cli.create_org(name, tier) -> int`, `list_orgs() -> int`, `create_key(org_id, name) -> int`, `list_keys(org_id: str | None) -> int`, `revoke_key(key_id) -> int`; `app.cli.main(argv: list[str] | None = None) -> int` (parses arguments and runs the matching coroutine with `asyncio.run`); console script `agentkit-server`.
- Shape: the commands are public async functions so tests can await them inside pytest-asyncio's loop; only `main()` calls `asyncio.run`, and it is covered by one subprocess test.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_cli.py`:

```python
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
```

The subprocess test needs the tables to exist in its throwaway SQLite file: have `main()` call
`await init_db()` when `DATABASE_URL` starts with `sqlite`, mirroring `main.py`'s dev-mode behaviour, before
dispatching the command.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && python3 -m pytest tests/test_cli.py -q`
Expected: collection error — `ImportError: cannot import name 'cli' from 'app'`.

- [ ] **Step 3: Implement `server/app/cli.py`**

```python
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


async def create_org(name: str, tier: str) -> int:
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


async def create_key(org_id: str, name: str) -> int:
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
```

- [ ] **Step 4: Register the console script** — in `server/pyproject.toml`, after `[project.optional-dependencies]`:

```toml
[project.scripts]
agentkit-server = "app.cli:main"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && python3 -m pytest tests/test_cli.py -q` → PASS. Then the full server suite and `ruff check app tests`.

- [ ] **Step 6: Commit**

```bash
git add server/app/cli.py server/pyproject.toml server/tests/test_cli.py
git commit -m "feat(server): agentkit-server CLI for orgs and API keys"
```

---

### Task 3: Container image and compose stack

**Files:**
- Create: `server/Dockerfile`, `server/docker-entrypoint.sh`, `server/docker-compose.yml`, `server/.env.example`, `server/.dockerignore`

**Interfaces:**
- Consumes: `agentkit-server` (Task 2), `python -m app.worker` (Task 1).
- Produces: compose project `agentkit` with services `db`, `api`, `worker`; API published on `127.0.0.1:${API_PORT:-8020}`; env contract `POSTGRES_PASSWORD`, `API_PORT`, `AGENTKIT_SIGNING_KEY`, optional `SMTP_*`.

- [ ] **Step 1: `server/Dockerfile`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 app

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install . \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R app:app /app

USER app
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

- [ ] **Step 2: `server/docker-entrypoint.sh`**

```sh
#!/bin/sh
# Apply migrations (API container only), then run the given command.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "agent-kit Cloud: applying database migrations"
  alembic upgrade head
fi

exec "$@"
```

- [ ] **Step 3: `server/docker-compose.yml`**

```yaml
name: agentkit

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: agentkit
      POSTGRES_DB: agentkit
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in server/.env}
    volumes:
      - agentkit-db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agentkit -d agentkit"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped

  api:
    build: .
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql+asyncpg://agentkit:${POSTGRES_PASSWORD}@db/agentkit
      ENABLE_ALERT_WORKER: "0"
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "127.0.0.1:${API_PORT:-8020}:8000"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')\""]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  worker:
    build: .
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql+asyncpg://agentkit:${POSTGRES_PASSWORD}@db/agentkit
      RUN_MIGRATIONS: "0"
    command: ["python", "-m", "app.worker"]
    depends_on:
      db:
        condition: service_healthy
      api:
        condition: service_started
    restart: unless-stopped

volumes:
  agentkit-db:
```

- [ ] **Step 4: `server/.env.example`**

```bash
# Copy to server/.env and fill in. Never commit .env.
POSTGRES_PASSWORD=change-me
API_PORT=8020

# Ed25519 seed for signing evidence bundles and deletion receipts (base64, 32 bytes):
#   python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
AGENTKIT_SIGNING_KEY=

# Email alerts (optional)
# SMTP_HOST=
# SMTP_PORT=587
# SMTP_SECURITY=starttls
# SMTP_USERNAME=
# SMTP_PASSWORD=
# SMTP_FROM=alerts@example.com
```

- [ ] **Step 5: `server/.dockerignore`**

```
tests/
.venv/
__pycache__/
*.pyc
*.db
*.db-journal
.env
```

- [ ] **Step 6: Verify what can be checked without Docker**

```bash
cd server && sh -n docker-entrypoint.sh && python3 check_compose.py && rm check_compose.py
```

where `check_compose.py` (written for this check, then deleted) is:

```python
import yaml

compose = yaml.safe_load(open("docker-compose.yml"))
api = compose["services"]["api"]
assert compose["name"] == "agentkit"
assert set(compose["services"]) == {"db", "api", "worker"}
assert api["ports"][0].startswith("127.0.0.1:")      # never published publicly
assert api["environment"]["ENABLE_ALERT_WORKER"] == "0"
assert compose["services"]["worker"]["command"] == ["python", "-m", "app.worker"]
assert compose["services"]["worker"]["environment"]["RUN_MIGRATIONS"] == "0"
assert "agentkit-db" in compose["volumes"]
print("compose file ok")
```

(`pyyaml` ships with the server's dev dependencies through uvicorn; `pip install pyyaml` in the venv if the
import fails.)

- [ ] **Step 7: Commit**

```bash
git add server/Dockerfile server/docker-entrypoint.sh server/docker-compose.yml server/.env.example server/.dockerignore
git commit -m "feat(server): Dockerfile and compose stack (api, worker, postgres)"
```

---

### Task 4: Documentation that matches the files

**Files:**
- Modify: `server/../docs/self-hosting.md` (i.e. `docs/self-hosting.md`)
- Modify: `PROJECT_INDEX.md`, `PROJECT_INDEX.json` (new server files and CLI)

- [ ] **Step 1: Rewrite `docs/self-hosting.md`**

Sections, in order:

1. **What runs** — the three containers and the one public entry point, with the diagram from the spec.
2. **Quick start** — clone; `cp server/.env.example server/.env`; generate the Postgres password and signing key with the two `python3 -c` one-liners; `docker compose -f server/docker-compose.yml up -d --build`; `curl http://127.0.0.1:8020/healthz`.
3. **Create your first org and key** — replaces the `scripts/seed_org.py` section that describes a file which does not exist:
   ```bash
   docker compose -f server/docker-compose.yml exec api agentkit-server create-org "My Org"
   docker compose -f server/docker-compose.yml exec api agentkit-server create-key <org-id> --name laptop
   ```
   then point the SDK at it: `export AGENTKIT_BASE_URL=http://127.0.0.1:8020` and `AGENTKIT_API_KEY=<key>`.
4. **Put it behind TLS** — the Caddy block (`agentkit.example.com { reverse_proxy 127.0.0.1:8020 }`) and the nginx equivalent; note that the API deliberately binds to localhost.
5. **Environment variables** — table of `DATABASE_URL`, `API_PORT`, `POSTGRES_PASSWORD`, `AGENTKIT_SIGNING_KEY`, `ENABLE_ALERT_WORKER`, `RUN_MIGRATIONS`, `SMTP_*`.
6. **Upgrades and migrations** — `git pull && docker compose ... up -d --build` (the entrypoint migrates); `alembic` commands for manual use.
7. **Alert worker** — one container, `ENABLE_ALERT_WORKER=0` on the API, and why running several evaluators double-fires alerts.
8. **Backups** — `docker compose exec db pg_dump -U agentkit agentkit > backup.sql`, and restore.
9. **Kubernetes** — keep the existing manifest sketch, prefixed: "Untested sketch — the compose stack above is what we run."

Delete the inline Dockerfile from the old Docker section and point at `server/Dockerfile` instead.

- [ ] **Step 2: Update the project index**

`PROJECT_INDEX.md`: add to the server tree `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`, `app/cli.py` (`agentkit-server`), `app/worker.py`; add `agentkit-server` to Entry Points; bump the server test count.
`PROJECT_INDEX.json`: add `"server_cli": "agentkit-server = app.cli:main (create-org, list-orgs, create-key, list-keys, revoke-key)"`, add the new files to `server_modules`, and update `tests.server.collected` / `files`.

- [ ] **Step 3: Commit**

```bash
git add docs/self-hosting.md PROJECT_INDEX.md PROJECT_INDEX.json
git commit -m "docs: self-hosting guide matches the shipped deployment files"
```

---

### Task 5: Deploy on rising and smoke-test

This task runs against the live box. Everything before it is committed and pushed first (`git push origin main`), because rising deploys from GitHub.

**Files:**
- Create: `docs/deploy-rising.md` (the exact steps used, so the deploy is repeatable)

- [ ] **Step 1: Check DNS before touching Caddy**

```bash
getent hosts agentkit.eudaimonia.win
```
Expected: `45.77.104.159`. If it does not resolve, stop and tell Alex the A record is missing — Caddy cannot issue a certificate without it. The rest of this task still works over `127.0.0.1`; only Steps 5–6 need DNS.

- [ ] **Step 2: Clone and configure on rising**

```bash
ssh rising 'sudo mkdir -p /opt/agentkit && sudo chown linuxuser:linuxuser /opt/agentkit &&
  git clone https://github.com/maco144/agent-kit.git /opt/agentkit 2>/dev/null || (cd /opt/agentkit && git pull --ff-only)'
ssh rising 'cd /opt/agentkit/server && cp -n .env.example .env && python3 - <<"EOF"
import base64, os, pathlib, secrets
p = pathlib.Path(".env"); text = p.read_text()
text = text.replace("POSTGRES_PASSWORD=change-me", "POSTGRES_PASSWORD=" + secrets.token_urlsafe(24))
text = text.replace("AGENTKIT_SIGNING_KEY=", "AGENTKIT_SIGNING_KEY=" + base64.b64encode(os.urandom(32)).decode())
p.write_text(text); print("wrote .env")
EOF
chmod 600 .env'
```

- [ ] **Step 3: Build and start**

```bash
ssh rising 'cd /opt/agentkit/server && docker compose config >/dev/null && docker compose up -d --build'
ssh rising 'cd /opt/agentkit/server && docker compose ps && curl -fsS http://127.0.0.1:8020/healthz'
```
Expected: three services `running` (db healthy), and `{"status":"ok"}`.

- [ ] **Step 4: Create the AIOS org and key**

```bash
ssh rising 'cd /opt/agentkit/server && docker compose exec -T api agentkit-server create-org "Rising Sun"'
ssh rising 'cd /opt/agentkit/server && docker compose exec -T api agentkit-server create-key <org-id> --name aios'
```
Write the key to `/opt/agentkit/server/aios-key.txt` on rising with `chmod 600` (it is needed by the AIOS agents later, and is not recoverable). Do not paste it into the conversation.

- [ ] **Step 5: Caddy and TLS** (skip if Step 1 found no DNS)

```bash
ssh rising 'sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S) &&
  printf "\nagentkit.eudaimonia.win {\n\treverse_proxy 127.0.0.1:8020\n}\n" | sudo tee -a /etc/caddy/Caddyfile >/dev/null &&
  sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy'
curl -fsS https://agentkit.eudaimonia.win/healthz
```
Expected: `{"status":"ok"}` over HTTPS. If `caddy validate` fails, restore the backup and stop.

- [ ] **Step 6: Smoke test with the real SDK**

On rising, in a throwaway venv, report a run through `CloudReporter` (no model key needed — the reporter and `AuditChain` are enough), then read it back:

```bash
ssh rising 'cd /opt/agentkit && python3 -m venv /tmp/ak && /tmp/ak/bin/pip -q install agent-kit-ai && cat > /tmp/smoke.py <<"EOF"
import asyncio, os
from agent_kit.audit.chain import AuditChain
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.types import AgentResult, CostSummary, Message, Turn

async def main():
    reporter = CloudReporter(project="aios", agent_name="smoke-test")  # AGENTKIT_BASE_URL + AGENTKIT_API_KEY
    chain = AuditChain()
    chain.append("agent_start", actor="smoke", payload={"prompt_preview": "hello"})
    turn = Turn(message_out=Message(role="assistant", content="hi"),
                cost=CostSummary(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001,
                                 model="claude-opus-5"))
    await reporter.on_run_start(run_id="smoke-1", model="claude-opus-5", prompt="hello")
    await reporter.on_turn_complete("smoke-1", turn, 0)
    chain.append("agent_complete", actor="smoke", payload={"turns": 1})
    result = AgentResult(output="hi", turns=[turn], total_cost_usd=0.001, total_tokens=15,
                         run_id="smoke-1", audit_root_hash=chain.root_hash())
    await reporter.on_run_complete("smoke-1", result)
    await reporter.on_audit_flush(run_id="smoke-1", events=chain.events(), final_root_hash=chain.root_hash())
    await reporter.flush(); await reporter.close()

asyncio.run(main())
EOF
AGENTKIT_BASE_URL=http://127.0.0.1:8020 AGENTKIT_API_KEY=$(cat server/aios-key.txt) /tmp/ak/bin/python /tmp/smoke.py && echo sent'
```

Then verify, using the key from the file:

```bash
ssh rising 'cd /opt/agentkit/server && K=$(cat aios-key.txt) &&
  curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/metrics/summary &&
  curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/audit/runs &&
  curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/audit/runs/smoke-1/verify'
```
Expected: summary counts the run, the run is listed with `chain_origin: "client"`, and verify reports the chain intact.

- [ ] **Step 7: Worker and restart checks**

```bash
ssh rising 'cd /opt/agentkit/server && docker compose logs --tail 20 worker'
ssh rising 'cd /opt/agentkit/server && docker compose restart && sleep 20 && docker compose ps &&
  K=$(cat aios-key.txt) && curl -fsS -H "Authorization: Bearer $K" http://127.0.0.1:8020/v1/audit/runs | head -c 200'
```
Expected: the worker logs its start line and no repeated errors; after a restart the services come back and the smoke run is still there (the volume persists).

- [ ] **Step 8: Write `docs/deploy-rising.md`**

Record: host and user, path `/opt/agentkit`, port 8020, the compose commands, where the key file lives, the Caddy block and backup convention, how to upgrade (`git pull && docker compose up -d --build`), and how to roll back (`docker compose down` plus the Caddyfile backup). Note that the AIOS agents use `AGENTKIT_BASE_URL=http://127.0.0.1:8020` so their traffic never leaves the box.

- [ ] **Step 9: Commit and push**

```bash
git add docs/deploy-rising.md
git commit -m "docs: rising deployment runbook"
git push origin main
```
