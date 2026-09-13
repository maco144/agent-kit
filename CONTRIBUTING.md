# Contributing to agent-kit

## License first

agent-kit is released under the [Rising Sun License v1.0](LICENSE). Personal, educational, and research use is free and unconditional. If you build something that generates revenue with it, you connect it to the Nous network. Contributions are accepted under those same terms — by opening a PR you agree your contribution ships under the Rising Sun License.

## Repository layout

Two independently deployable components, each with its own `pyproject.toml`, test suite, and lint config:

| Path | What it is | Install |
|---|---|---|
| `agent_kit/` | The SDK — the pip package | `pip install -e ".[dev]"` |
| `server/` | agent-kit Cloud backend (FastAPI) | `cd server && pip install -e ".[dev]"` |

`PROJECT_INDEX.md` is a 3KB map of the whole repo. Read it before going file-hunting.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,openai,otel]"    # SDK
pip install -e "./server[dev]"         # Cloud server
```

## The checks that must pass

CI runs exactly these on Python 3.11 and 3.12. Run them locally before pushing.

```bash
# SDK
ruff check agent_kit tests
mypy agent_kit
pytest

# Cloud server
cd server
ruff check app tests
pytest
DATABASE_URL=sqlite+aiosqlite:///./ci.db alembic upgrade head
```

`mypy` runs in strict mode against `agent_kit`. New code is expected to type-check clean — no `# type: ignore` without a comment explaining why.

## Conventions

These are load-bearing. Breaking them breaks something downstream:

- **`agent_kit/types.py` is the import-graph root.** It imports nothing from `agent_kit`. Every other module imports upward from it. This is what keeps circular imports impossible — never add an internal import there.
- **Async throughout.** `Agent.run()`, `Agent.stream()`, every provider method, every tool function, and every server route handler is `async`.
- **Pydantic v2 for all public models.** Use `Field(default_factory=...)` for mutable defaults.
- **`AgentConfig` defaults are production-safe** (retry 3 attempts, circuit breaker threshold 5, audit on). Don't weaken them without a stated reason.
- **`ToolRegistry` enforces `allowed_tools` at call time**, not as advice. A disallowed tool raises `ToolNotAllowedError`.
- **`CloudReporter` is fire-and-forget, always.** It must never block the agent or propagate an exception into the hot path.
- **Optional providers are lazily imported** (`openai`, `ollama`) so a missing extra is never an import-time error.

## Testing

- `pytest-asyncio` runs in `asyncio_mode = "auto"` in both suites — do not add `@pytest.mark.asyncio`.
- SDK: test provider adapters by injecting a fake client that records request kwargs — see `tests/test_provider_requests.py` — and assert the exact payload the SDK would send. Don't use `respx` for Anthropic: `anthropic>=1.0` uses `httpx2`, which respx doesn't intercept, so requests silently reach the network. Agent-loop behaviour can use `MockProvider` from `tests/conftest.py`.
- Server: tests run against a real in-process SQLite database via `aiosqlite`. Do not mock the DB layer.
- Fixtures live in `tests/conftest.py` and `server/tests/conftest.py`.

## Adding a provider

1. Subclass `BaseProvider` in `agent_kit/providers/<name>.py`.
2. Implement `complete()`, `stream()`, and `name()`. `stream()` is an async generator — `async def` with `yield`.
3. Lazy-import it in `agent_kit/providers/__init__.py` as `get_<name>_provider()`.
4. Add the optional dependency to `pyproject.toml` under `[project.optional-dependencies]`.
5. Add `respx`-based tests covering both `complete()` and `stream()`.

## Adding a server endpoint

1. Add the route to the relevant router in `server/app/routers/`.
2. Define request/response models in `server/app/schemas.py` — never return ORM objects directly.
3. If the schema changes, generate a migration: `cd server && alembic revision --autogenerate -m "..."`, then review the generated SQL by hand.
4. Document it in `docs/api-reference.md`. An endpoint that isn't in the reference doesn't exist.

## Pull requests

- One concern per PR. A relicense and a bugfix are two PRs.
- Update `CHANGELOG.md` under `## [Unreleased]`.
- Say what you ran and what it printed. "Tests pass" without output is not evidence.
