# agent-kit — Claude Code Guide

## Project

Production-ready Python framework for building AI agents. v0.2.0.
**License: FSL-1.1-Apache-2.0** (source-available; converts to Apache 2.0 two years after each release).

## Commands

```bash
# Install in editable mode with dev deps
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check agent_kit tests

# Type-check
mypy agent_kit
```

## Architecture

- **`agent_kit/types.py`** is the import graph root — no internal imports. All other modules import from it; never add internal imports here.
- **`agent_kit/agent/loop.py`** drives the turn loop: retry → circuit breaker → provider call → tool dispatch → audit.
- Optional providers (`openai`, `ollama`) are imported lazily to avoid hard dependency errors at import time.

## Key Conventions

- All public models are Pydantic v2 `BaseModel`. Use `Field(default_factory=...)` for mutable defaults.
- Async throughout — `Agent.run()`, `Agent.stream()`, all provider methods, and all tool functions must be `async`.
- The `@tool` decorator auto-generates JSON Schema from type hints. Parameters must be annotated; return type should be `dict` or a JSON-serialisable type.
- `AgentConfig` defaults are production-safe (retry=3 attempts, circuit breaker threshold=5, audit enabled). Don't weaken them without good reason.
- `ToolRegistry` enforces `allowed_tools` at call time, not just advisory — raising `ToolNotAllowedError` if the LLM tries a disallowed tool.

## Testing

- `pytest-asyncio` in `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Provider calls are mocked via `respx` (HTTP-level). Do not mock `BaseProvider` directly.
- Fixtures live in `tests/conftest.py`.

## Adding a Provider

1. Subclass `BaseProvider` in `agent_kit/providers/<name>.py`.
2. Implement `complete()`, `stream()`, and `name()`.
3. Lazy-import it in `agent_kit/providers/__init__.py` (`get_<name>_provider()`).
4. Add the optional dep to `pyproject.toml` under `[project.optional-dependencies]`.
