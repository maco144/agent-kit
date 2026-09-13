# Self-Hosting the agent-kit Cloud Backend

The `server/` directory contains the FastAPI backend that backs agent-kit Cloud. Run it yourself for air-gapped environments, compliance requirements, or cost control.

## Architecture overview

```
SDK (CloudReporter)
      │  POST /v1/events (gzip NDJSON)
      ▼
FastAPI server (server/)
      │
      ├── SQLite (dev/test)  ──or──  PostgreSQL (production)
      │
      ├── Alembic migrations (server/migrations/)
      └── Background alert worker (opt-in)
```

---

## Local development

```bash
cd server

# Install the server (editable, with dev tools)
pip install -e ".[dev]"

# Optional — defaults to sqlite+aiosqlite:///./agentkit_cloud.db
export DATABASE_URL="sqlite+aiosqlite:///./agentkit.db"

# Run migrations
alembic upgrade head

# Start the server
uvicorn app.main:app --reload --port 8000
```

The server is now live at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

---

## Create your first org and API key

The server has no sign-up UI yet — provision via the database directly or with a seed script:

```python
# scripts/seed_org.py
import asyncio, secrets, hashlib
from app.database import SessionLocal
from app.models import Organization, ApiKey

async def seed():
    raw_key = "akt_live_" + secrets.token_hex(24)
    hashed = hashlib.sha256(raw_key.encode()).hexdigest()

    async with SessionLocal() as db:
        org = Organization(name="My Org")
        db.add(org)
        await db.flush()
        db.add(ApiKey(org_id=org.id, name="default", key_prefix=raw_key[:13], key_hash=hashed))
        await db.commit()

    print(f"API key: {raw_key}")
    print("Add to CloudReporter: CloudReporter(api_key=..., base_url='http://localhost:8000')")

asyncio.run(seed())
```

```bash
python scripts/seed_org.py
```

---

## Production deployment

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes (production) | `sqlite+aiosqlite:///./agentkit_cloud.db` | SQLAlchemy async URL (e.g. `postgresql+asyncpg://user:pass@host/db`). SQLite URLs auto-create tables on startup; anything else expects `alembic upgrade head`. |
| `ENABLE_ALERT_WORKER` | No | unset | `1` or `true` runs the 60-second alert evaluator in this process |
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

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server/ .
RUN pip install --no-cache-dir .
ENV DATABASE_URL="postgresql+asyncpg://agentkit:password@db/agentkit"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4"]
```

```bash
docker build -t agentkit-server .
docker run -p 8000:8000 \
  -e DATABASE_URL="postgresql+asyncpg://..." \
  agentkit-server
```

### Kubernetes (minimal)

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

The background alert worker evaluates polled alert rules (cost anomaly, error rate) every 60 seconds. It is opt-in to avoid unwanted side effects in test or read-only deployments.

```bash
ENABLE_ALERT_WORKER=1 uvicorn app.main:app ...
```

For production, run exactly one process with `ENABLE_ALERT_WORKER=1` to avoid duplicate evaluations. The worker starts per uvicorn worker process, so don't combine it with `--workers N` or multiple replicas — run a dedicated single-process deployment for it instead. The worker is safe to restart — it uses database state, not in-memory state.

Event-driven alerts (circuit breaker open, audit integrity failure) fire immediately via the ingest pipeline and do not require the worker.

---

## Health check

```bash
curl http://localhost:8000/healthz
# {"status": "ok"}
```

Use this as your load balancer health check endpoint. It does not touch the database.

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
