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

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL="sqlite+aiosqlite:///./agentkit.db"
export SECRET_KEY="change-me-in-production"

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
        db.add(ApiKey(org_id=org.id, key_hash=hashed, name="default"))
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
| `DATABASE_URL` | Yes | — | SQLAlchemy async URL (e.g. `postgresql+asyncpg://user:pass@host/db`) |
| `SECRET_KEY` | Yes | — | Used for internal signing |
| `ENABLE_ALERT_WORKER` | No | `""` | Set to `1` to run the 60-second alert evaluator |
| `LOG_LEVEL` | No | `INFO` | Python logging level |

### PostgreSQL

```bash
pip install asyncpg
export DATABASE_URL="postgresql+asyncpg://agentkit:password@localhost/agentkit"
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
ENV DATABASE_URL="postgresql+asyncpg://agentkit:password@db/agentkit"
ENV ENABLE_ALERT_WORKER="1"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4"]
```

```bash
docker build -t agentkit-server .
docker run -p 8000:8000 \
  -e DATABASE_URL="postgresql+asyncpg://..." \
  -e SECRET_KEY="..." \
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

## Alert worker

The background alert worker evaluates polled alert rules (cost anomaly, error rate) every 60 seconds. It is opt-in to avoid unwanted side effects in test or read-only deployments.

```bash
ENABLE_ALERT_WORKER=1 uvicorn app.main:app ...
```

For production, run exactly one instance with `ENABLE_ALERT_WORKER=1` to avoid duplicate evaluations. The worker is safe to restart — it uses database state, not in-memory state.

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
