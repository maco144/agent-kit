# agent-kit — Claude Code Guide

## Project

Production-ready Python framework for building AI agents, plus the agent-kit Cloud backend. v0.2.0.
**License: FSL-1.1-Apache-2.0** (source-available; converts to Apache 2.0 two years after each release).

Two independently deployable components:
- **`agent_kit/`** — SDK (pip package)
- **`server/`** — Cloud backend (FastAPI + SQLAlchemy + Alembic)

## Commands

```bash
# --- SDK ---
# Install in editable mode with dev deps
pip install -e ".[dev]"

# Run SDK tests
pytest

# Lint SDK
ruff check agent_kit tests

# Type-check SDK
mypy agent_kit

# --- Cloud Server ---
cd server

# Install server in editable mode with dev deps
pip install -e ".[dev]"

# Run server tests
pytest

# Lint server
ruff check app tests

# Start server (SQLite for local dev)
DATABASE_URL=sqlite+aiosqlite:///./dev.db uvicorn app.main:app --reload

# Run migrations
alembic upgrade head
```

## Architecture

### SDK (`agent_kit/`)
- **`agent_kit/types.py`** is the import graph root — no internal imports. All other modules import from it; never add internal imports here.
- **`agent_kit/agent/loop.py`** drives the turn loop: retry → circuit breaker → provider call → tool dispatch → audit → cloud report.
- **`agent_kit/cloud/reporter.py`** ships lifecycle events to the cloud backend. Fire-and-forget — never blocks the agent.
- Optional providers (`openai`, `ollama`) are imported lazily to avoid hard dependency errors at import time.

### Cloud Server (`server/`)
- **`server/app/main.py`** — FastAPI app entry point; starts background alert evaluation worker when `ENABLE_ALERT_WORKER=1`.
- **`server/app/routers/ingest.py`** — `POST /v1/events`; processes all SDK event types, populates metrics/audit tables, triggers alert evaluation.
- **`server/app/routers/metrics.py`** — Fleet dashboard API (`/summary`, `/cost`, `/runs`, `/agents`, `/circuit-breaker`, `/active`).
- **`server/app/routers/alerts.py`** — Alert rule + channel CRUD; firing history + ack workflow.
- **`server/app/routers/support.py`** — Support context sidebar + SLA definitions (free/pro/enterprise).
- **`server/app/alerting/`** — `evaluator.py` (rule evaluation, immediate fire helpers) + `dispatch.py` (email/Slack/PagerDuty/webhook).
- Database migrations live in `server/migrations/versions/` (4 Alembic versions).

## Key Conventions

- All public models are Pydantic v2 `BaseModel`. Use `Field(default_factory=...)` for mutable defaults.
- Async throughout — `Agent.run()`, `Agent.stream()`, all provider methods, all tool functions, and all server route handlers must be `async`.
- The `@tool` decorator auto-generates JSON Schema from type hints. Parameters must be annotated; return type should be `dict` or a JSON-serialisable type.
- `AgentConfig` defaults are production-safe (retry=3 attempts, circuit breaker threshold=5, audit enabled). Don't weaken them without good reason.
- `ToolRegistry` enforces `allowed_tools` at call time, not just advisory — raising `ToolNotAllowedError` if the LLM tries a disallowed tool.
- `CloudReporter` is always fire-and-forget. Never `await` its methods inside the hot path in a way that could propagate exceptions to the agent.

## Testing

### SDK
- `pytest-asyncio` in `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Provider calls are mocked via `respx` (HTTP-level). Do not mock `BaseProvider` directly.
- Fixtures live in `tests/conftest.py`.

### Cloud Server
- `pytest-asyncio` in `asyncio_mode = "auto"` — same rule, no decorator needed.
- Uses an in-process SQLite database (`aiosqlite`) — do not mock the DB layer.
- Fixtures live in `server/tests/conftest.py`; they stand up a real FastAPI `AsyncClient` + seeded `Organization`.

## Adding a Provider

1. Subclass `BaseProvider` in `agent_kit/providers/<name>.py`.
2. Implement `complete()`, `stream()`, and `name()`.
3. Lazy-import it in `agent_kit/providers/__init__.py` (`get_<name>_provider()`).
4. Add the optional dep to `pyproject.toml` under `[project.optional-dependencies]`.

---

## AIOS Portfolio Integration

agent-kit is part of the **Rising Sun** portfolio managed by the AIOS autonomous CEO engine at `eudaimonia.win`. Work items (research, content, outreach tasks) are dispatched here from the CEO cycle.

### Work Queue

Pending tasks live on the AIOS rising server. Fetch at session start:

```bash
# List pending tasks
curl http://eudaimonia.win:8000/api/v1/portfolio/companies/agent-kit/work?status=pending&limit=50

# Compact view
curl -s 'http://eudaimonia.win:8000/api/v1/portfolio/companies/agent-kit/work?status=pending&limit=50' \
  | python3 -c "import sys,json; [print(i['id'][:8], i['category'], '-', i['title'][:80]) for i in json.load(sys.stdin)['items']]"

# Mark a task done
curl -X POST "http://eudaimonia.win:8000/api/v1/company/work-queue/{item_id}/complete?note=what+was+done"
```

Work through tasks one at a time. Mark done immediately after each one is complete.

**Categories:** `infra` (packaging, deployment), `sdk-core` (agent loop, providers, tools), `research` (documentation, competitive analysis), `testing` (benchmarks, examples), `observability` (metrics, engagement)

### Rising (Production)

- **AIOS API:** `http://eudaimonia.win:8000`
- **agent-kit company memory DB:** `/data/eudaimonia/company_memory_agent-kit.db` (on rising)
- **Departments active:** research, content, outreach (no engineering sprint — CEO dispatches non-code tasks)

### Portfolio Config

- `companies/agent-kit/company.toml` in the AIOS repo — identity anchors, competitive landscape, content topics
- `portfolio.toml` in the AIOS repo — fleet registry (`active = true` to enable daily CEO cycle)
- Codebase path used by the CEO cycle: `/home/alex/agent-kit` (git log for engineering context)

### Shared Work Queue

This project's work queue (`company_id="agent-kit"`) lives in the **shared PostgreSQL** on rising — not a local SQLite file and not behind the kernel REST API at `:8000`.

**Check pending tasks:**
```bash
ssh rising "docker exec eudaimonia-eudaimonia-postgres-1 psql -U eudaimonia -c \
  \"SELECT id, title, status, priority FROM work_items WHERE company_id='agent-kit' AND status='pending' ORDER BY priority DESC\""
```

**Mark a task done:**
```bash
ssh rising "docker exec eudaimonia-eudaimonia-postgres-1 psql -U eudaimonia -c \
  \"UPDATE work_items SET status='done', completion_note='<note>' WHERE id='<uuid>'\""
```

**Do NOT** rely on `http://eudaimonia.win:8000` for work queue access — the kernel restarts frequently during upgrades and the API will timeout. Use PostgreSQL directly.
