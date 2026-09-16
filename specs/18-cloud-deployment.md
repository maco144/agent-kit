# Spec 18 — Cloud Deployment (phase 1)

Status: **approved** · Written 2026-09-15 · Cloud phase 1 (see `docs/self-hosting.md`)

## Goal

agent-kit Cloud is code that passes tests but has never run anywhere. Phase 1 makes it deployable by anyone —
including us — and runs it on `rising`, where the AIOS agents that will report to it already live.

**Done means:** `docker compose up -d --build` in `server/` brings up Postgres, the API, and one alert worker;
`agentkit-server create-org "Rising Sun"` and `agentkit-server create-key <org-id> --name aios` print an API key
that authenticates against the API; on rising the stack answers at `https://agentkit.eudaimonia.win/healthz`
through the existing Caddy; an agent on the box reports a run with `CloudReporter(base_url=...)` and the run comes
back from `/v1/metrics/summary` and `/v1/audit/runs` with a chain that verifies; the worker logs an evaluation
pass every 60 seconds; and `docs/self-hosting.md` describes only files that exist.

## Decisions

1. **The alert worker is its own container**, not a thread inside each web process. The API runs with
   `ENABLE_ALERT_WORKER=0`; exactly one `worker` service evaluates alerts, budgets, and retention purges. This
   closes the double-evaluation gap that has existed since alerting shipped.
2. **The API binds to `127.0.0.1` on the host.** Caddy is the only public entry point.
3. **Postgres runs in the agent-kit compose project** with its own named volume — the same file self-hosters run,
   and agent-kit data stays out of the AIOS database server.
4. **Admin actions are a CLI in the server package** (`agentkit-server`), not a snippet in the docs. Keys are
   printed once and stored only as SHA-256 hashes, matching the existing `ApiKey` model.
5. **Migrations run at container start** (`alembic upgrade head`), so a fresh volume becomes a working database
   with no manual step.
6. **Secrets live in `server/.env`** on the host (git-ignored), never in the repo or the image.

## Files

| File | Responsibility |
|---|---|
| `server/Dockerfile` | `python:3.12-slim`; installs the server package; non-root `app` user; entrypoint script |
| `server/docker-entrypoint.sh` | `alembic upgrade head`, then `exec` the given command (api or worker) |
| `server/docker-compose.yml` | `db` (postgres:16-alpine, named volume, healthcheck), `api` (published `127.0.0.1:${API_PORT}:8000`), `worker` (same image, `python -m app.worker`) |
| `server/.env.example` | `POSTGRES_PASSWORD`, `DATABASE_URL`, `API_PORT`, `AGENTKIT_SIGNING_KEY`, SMTP settings |
| `server/.dockerignore` | tests, `.venv`, `*.db`, `.env` |
| `server/app/worker.py` | `run_forever()` loop + `python -m app.worker` entry point |
| `server/app/cli.py` | `agentkit-server` console script |
| `server/pyproject.toml` | `[project.scripts] agentkit-server = "app.cli:main"` |
| `docs/self-hosting.md` | Rewritten around the real files and commands |
| `docs/deploy-rising.md` | The exact steps used on rising, so the deploy is repeatable |

## Behaviour

### `agentkit-server` CLI

```
agentkit-server create-org <name>                 # prints the org id
agentkit-server list-orgs
agentkit-server create-key <org-id> --name <label>  # prints the key ONCE (akt_live_<48 hex>)
agentkit-server list-keys [--org <org-id>]        # id, name, prefix, created, last used
agentkit-server revoke-key <key-id>
```

Every command takes `DATABASE_URL` from the environment, runs through the same async SQLAlchemy session as the
app, exits non-zero with a one-line message on failure (unknown org or key), and prints nothing secret except the
one-time key. Keys are `"akt_live_" + secrets.token_hex(24)`; the row stores `key_prefix` (first 13 characters)
and the SHA-256 hash, matching `app/auth.py`.

### Worker

`app/worker.py` holds the loop that `main.py` runs today: every 60 seconds, `evaluate_all_rules`,
`evaluate_all_budgets`, and `purge_expired`, each cycle in its own session, with exceptions logged and the loop
continuing. `main.py` imports the same function for its opt-in in-process mode, so there is one implementation.
`python -m app.worker` runs it standalone and logs "alert worker cycle complete" at debug level each pass.

### Compose

- `db`: `postgres:16-alpine`, `POSTGRES_USER=agentkit`, `POSTGRES_DB=agentkit`, password from `.env`, volume
  `agentkit-db`, `pg_isready` healthcheck.
- `api`: built from `server/`, `depends_on: db (healthy)`, `ENABLE_ALERT_WORKER=0`, published on
  `127.0.0.1:${API_PORT:-8020}:8000`, healthcheck on `/healthz`, `restart: unless-stopped`, `--workers 4`.
- `worker`: same image, `command: python -m app.worker`, `depends_on: db (healthy)`, no published port.
- Both app services read `DATABASE_URL=postgresql+asyncpg://agentkit:${POSTGRES_PASSWORD}@db/agentkit`.

### rising

1. Clone to `/opt/agentkit`; write `server/.env` with a generated Postgres password and an Ed25519
   `AGENTKIT_SIGNING_KEY`; `API_PORT=8020` (free — the box already uses 8000, 8001, 8010–8012, 8040, 8101).
2. `docker compose -f server/docker-compose.yml -p agentkit up -d --build`.
3. Caddy: back up `/etc/caddy/Caddyfile`, append
   `agentkit.eudaimonia.win { reverse_proxy 127.0.0.1:8020 }`, run `caddy validate`, then reload. DNS: an A
   record for `agentkit` → 45.77.104.159 must exist first (Alex adds it).
4. Create the first org and key; store the key for the AIOS agents.

## Testing

`server/tests/test_cli.py` (in CI with the rest):

- `create-org` inserts an organization and prints its id.
- `create-key` prints a key starting `akt_live_`, stores only its hash and prefix, and the key authenticates
  against an API request.
- `revoke-key` deletes the key and the API then rejects it with 401.
- `list-orgs` / `list-keys` print the rows; `list-keys --org` filters.
- Unknown org or key ids exit non-zero with a message and no traceback.

`server/tests/test_worker.py`: one cycle of the worker loop evaluates rules, budgets, and retention against the
test database, and an exception inside a cycle is logged without stopping the loop.

Live on rising, after deploy: `/healthz`; create org + key; a real `CloudReporter` run from the box reaching
`/v1/metrics/summary`, `/v1/audit/runs`, and `/v1/audit/runs/{id}/verify`; `docker compose logs worker` showing
repeated cycles; `docker compose restart` leaving data intact.

## Out of scope

- Signup, key self-service, quotas, billing (phase 2); dashboard (phase 3).
- Backups, restore drills, and monitoring of the service itself (phase 4).
- Moving off rising to managed hosting.
- Kubernetes manifests: the existing sketch in `docs/self-hosting.md` stays as a sketch, marked untested.
