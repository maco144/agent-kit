# Self-Hosting the agent-kit Cloud Backend

The `server/` directory contains the FastAPI backend that backs agent-kit Cloud. Run it yourself for air-gapped environments, compliance requirements, or cost control.

## What runs

Three containers, one public entry point:

```
 SDK (CloudReporter) · Claude/OpenAI adapters · OTLP exporters
                   │  HTTPS
                   ▼
        reverse proxy (Caddy/nginx, yours)
                   │  127.0.0.1:8020
       ┌───────────┴───────────┐
       ▼                       ▼
  api (uvicorn)          worker (alerts, budgets, retention)
       │                       │
       └────────▶ postgres ◀───┘   (named volume: agentkit-db)
```

The API container applies Alembic migrations at start. The worker is a separate container so exactly one
process evaluates alerts, however many API workers are serving traffic.

---

## Quick start (Docker)

```bash
git clone https://github.com/maco144/agent-kit.git
cd agent-kit/server
cp .env.example .env

# Fill in the two secrets
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
python3 -c "import base64, os; print('AGENTKIT_SIGNING_KEY=' + base64.b64encode(os.urandom(32)).decode())"
$EDITOR .env

docker compose up -d --build
curl http://127.0.0.1:8020/healthz     # {"status":"ok"}
```

`docker compose ps` shows `db`, `api`, and `worker`. The API is published on `127.0.0.1` only — put a reverse
proxy in front of it to reach it from anywhere else (see [Put it behind TLS](#put-it-behind-tls)).

---

## Local development (no Docker)

```bash
cd server
pip install -e ".[dev]"

export DATABASE_URL="sqlite+aiosqlite:///./agentkit.db"   # or a postgresql+asyncpg:// URL
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Interactive API docs at `http://localhost:8000/docs`. For the alert worker in this mode, either run
`python -m app.worker` in a second terminal or start the API with `ENABLE_ALERT_WORKER=1`.

---

## Create your first org and API key

The server has no sign-up UI yet. Use the `agentkit-server` CLI that ships in the image:

```bash
docker compose exec api agentkit-server create-org "My Org"
# created org 'My Org' (free) with id 6f1e...  ← copy the id

docker compose exec api agentkit-server create-key 6f1e... --name laptop
# store this key now - it cannot be shown again:
#   akt_live_...
```

Other commands: `list-orgs`, `list-keys [--org <id>]`, `revoke-key <key-id>`. Keys are stored as SHA-256
hashes with a 13-character prefix for identification, so a key is recoverable only at creation time.

Point an agent at the server:

```bash
export AGENTKIT_BASE_URL=http://127.0.0.1:8020
export AGENTKIT_API_KEY=akt_live_...
```

Without Docker, run the same commands directly: `agentkit-server create-org "My Org"` (it reads
`DATABASE_URL` from the environment, like the server).

---

## Production deployment

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes (production) | `sqlite+aiosqlite:///./agentkit_cloud.db` | SQLAlchemy async URL (e.g. `postgresql+asyncpg://user:pass@host/db`). SQLite URLs auto-create tables on startup; anything else expects `alembic upgrade head`. |
| `ENABLE_ALERT_WORKER` | No | unset | `1` or `true` runs the 60-second worker inside the API process (local development). The compose stack sets `0` and runs `worker` as its own container |
| `POSTGRES_PASSWORD` | Yes (compose) | unset | Password for the bundled Postgres; compose builds `DATABASE_URL` from it |
| `API_PORT` | No | `8020` | Host port the API is published on, bound to `127.0.0.1` |
| `RUN_MIGRATIONS` | No | `1` | `0` skips `alembic upgrade head` in the entrypoint (the worker container sets this) |
| `AGENTKIT_SIGNING_KEY` | Recommended | unset | Base64 32-byte Ed25519 seed that signs evidence bundles and deletion receipts; never stored. Generate with `python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`. Unset → a key is generated and its seed stored in the database |
| `AGENTKIT_SIGNING_KEY_ID` | No | derived | Key ID published for the env key (default `ak-` + 12 hex of its public key hash). Changing the key retires the old one but keeps it published |
| `SMTP_HOST` | For email alerts | unset | SMTP server. Unset = email channels log instead of sending |
| `SMTP_PORT` | No | `587` (`465` with `ssl`) | SMTP port |
| `SMTP_SECURITY` | No | `starttls` | `starttls`, `ssl` (implicit TLS), or `none` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | No | unset | Credentials; login is skipped when `SMTP_USERNAME` is unset |
| `SMTP_FROM` | No | `agent-kit <alerts@localhost>` | `From:` header — set this to a domain your SMTP relay is allowed to send as |

Webhook channels sign deliveries with their own per-channel `secret` (see [API reference](api-reference.md#webhook-signatures)); there is no server-wide signing key.

### PostgreSQL

```bash
export DATABASE_URL="postgresql+asyncpg://agentkit:password@localhost/agentkit"
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Docker

The image and stack ship in `server/`: `Dockerfile`, `docker-entrypoint.sh` (runs `alembic upgrade head`,
then the given command), `docker-compose.yml`, and `.env.example`. Build and run them with the Quick start
above, or build the image alone:

```bash
docker build -t agentkit-server server/
```

Environment contract for the compose stack: `POSTGRES_PASSWORD` (required), `API_PORT` (default 8020),
`AGENTKIT_SIGNING_KEY`, and any `SMTP_*` settings. `RUN_MIGRATIONS=0` skips migrations for a container (the
worker uses this so only the API migrates).

### Put it behind TLS

The API binds to `127.0.0.1` on purpose — terminate TLS in a reverse proxy:

```caddyfile
agentkit.example.com {
	reverse_proxy 127.0.0.1:8020
}
```

nginx equivalent:

```nginx
server {
    listen 443 ssl;
    server_name agentkit.example.com;
    location / {
        proxy_pass http://127.0.0.1:8020;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### Kubernetes (untested sketch)

The compose stack above is what we run. This manifest is a starting point, not a tested deployment.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agentkit-server
spec:
  replicas: 2
  template:
    spec:
      initContainers:
        - name: migrate
          image: agentkit-server:latest
          command: ["alembic", "upgrade", "head"]
          envFrom: [{secretRef: {name: agentkit-secrets}}]
      containers:
        - name: server
          image: agentkit-server:latest
          command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
          envFrom: [{secretRef: {name: agentkit-secrets}}]
          ports: [{containerPort: 8000}]
          readinessProbe:
            httpGet: {path: /healthz, port: 8000}
```

---

## Migrations

Migrations live in `server/migrations/versions/` and are managed with Alembic.

```bash
# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1

# Show current revision
alembic current

# Generate a new migration after model changes
alembic revision --autogenerate -m "describe change"
```

Migration history:

| Revision | Description |
|---|---|
| `001` | Initial schema: organizations, api_keys, audit_runs, audit_events |
| `002` | Metrics schema: active_run_cache, agent_metric_snapshots, circuit_breaker_events |
| `003` | Alerting: alert_channels, alert_rules, alert_firings |
| `004` | Support tiers: adds `tier` and `plan_metadata` to organizations |
| `005` | OTLP ingest: `audit_runs.chain_origin`, `active_run_cache.last_event_at` / `failure_message` |
| `006` | Fleet budgets: `budgets` |
| `007` | Compliance: `signing_keys`, `legal_holds`, `deletion_receipts`, `organizations.audit_retention_days` |

---

## Email alerts (SMTP)

Email channels deliver through any SMTP relay (SES, Postmark, SendGrid, Mailgun, your own MTA). Set `SMTP_HOST` and credentials:

```bash
export SMTP_HOST="email-smtp.us-east-1.amazonaws.com"
export SMTP_USERNAME="..."
export SMTP_PASSWORD="..."
export SMTP_FROM="agent-kit <alerts@yourcompany.com>"
```

Then create a channel with `{"type": "email", "config": {"to": ["oncall@yourcompany.com"]}}` — a test email is sent on creation, and `POST /v1/alerts/channels/{id}/test` returns `{"sent": false, "error": ...}` if the relay rejects it. Without `SMTP_HOST`, email notifications are logged and dropped, which is the right behaviour for local dev and tests.

---

## Alert worker

One container (`python -m app.worker`) evaluates polled alert rules (cost anomaly, error rate), fleet budgets,
and audit retention purges every 60 seconds. The compose stack runs exactly one, and sets
`ENABLE_ALERT_WORKER=0` on the API.

Run more than one evaluator and every polled alert fires once per evaluator, so keep it to a single container
(or a single process with `ENABLE_ALERT_WORKER=1` outside Docker). The worker is safe to restart: it keeps no
in-memory state.

Event-driven alerts (circuit breaker open, audit integrity failure, flagged tool output) fire immediately
through the ingest pipeline and do not need the worker.

```bash
docker compose logs -f worker
```

---

## Health check

```bash
curl http://localhost:8000/healthz
# {"status": "ok"}
```

Use this as your load balancer health check endpoint. It does not touch the database.

---

## Backups

```bash
docker compose exec db pg_dump -U agentkit agentkit > agentkit-$(date +%F).sql          # back up
cat agentkit-2026-09-16.sql | docker compose exec -T db psql -U agentkit -d agentkit    # restore
```

The database holds the audit chains your evidence bundles are built from, so back it up on the same schedule
as anything else you would have to produce for an auditor.

---

## Pointing CloudReporter at your server

```python
from agent_kit.cloud import CloudReporter

reporter = CloudReporter(
    api_key="akt_live_...",
    base_url="https://agentkit.internal.mycompany.com",
    project="production",
    agent_name="billing-agent",
)
```
