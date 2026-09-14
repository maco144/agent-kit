"""
Claude Agent SDK adapter — report Claude Agent SDK runs to agent-kit Cloud.

Usage::

    from claude_agent_sdk import ClaudeAgentOptions, query
    from agent_kit.cloud import CloudReporter
    from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver

    observer = ClaudeAgentObserver(CloudReporter(project="support"))
    options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Grep"]))

    async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
        ...  # every message arrives unchanged

Each ``observe()`` call is one agent-kit run. Hooks only observe: they return no
decision and never change tool input or output. Hook events for a session that isn't
being observed are ignored, because cost and completion come from the message stream.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from typing import TYPE_CHECKING, Any

from agent_kit.integrations.recorder import RunRecorder

if TYPE_CHECKING:
    from claude_agent_sdk import HookJSONOutput

    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.integrations")

HARNESS = "claude-agent-sdk"
_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
)


class ClaudeAgentObserver:
    """Observes Claude Agent SDK runs through hooks and the message stream."""

    def __init__(self, reporter: CloudReporter, agent_name: str | None = None) -> None:
        self._recorder = RunRecorder(reporter, harness=HARNESS, agent_name=agent_name or "claude-agent")
        self._sessions: dict[str, _Observation] = {}  # session_id -> active observe() call
        self._tool_started: dict[str, float] = {}  # tool_use_id -> monotonic start
        self._lock = threading.Lock()

    def hooks(self) -> dict[str, list[Any]]:
        """Hook matchers for ``ClaudeAgentOptions.hooks``. Prefer ``with_hooks`` to merge them."""
        from claude_agent_sdk import HookMatcher

        return {event: [HookMatcher(hooks=[self._on_hook])] for event in _HOOK_EVENTS}

    def with_hooks(self, options: Any) -> Any:
        """Add agent-kit's hooks to ``options``, after any hooks already configured."""
        merged: dict[str, list[Any]] = {
            event: list(matchers) for event, matchers in (options.hooks or {}).items()
        }
        for event, matchers in self.hooks().items():
            merged.setdefault(event, []).extend(matchers)
        options.hooks = merged
        return options

    async def observe(
        self, messages: AsyncIterable[Any], prompt: str | None = None
    ) -> AsyncIterator[Any]:
        """Yield every message from ``messages`` unchanged while recording the run."""
        run = _Observation(self, str(uuid.uuid4()), prompt)
        try:
            async for message in messages:
                run.on_message(message)
                yield message
        except Exception as exc:
            run.fail(type(exc).__name__, str(exc))
            raise
        finally:
            run.close()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    async def _on_hook(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> HookJSONOutput:
        try:
            self._record_hook(input_data, tool_use_id)
        except Exception:
            logger.debug("ClaudeAgentObserver hook failed", exc_info=True)
        return {}

    def _record_hook(self, data: dict[str, Any], tool_use_id: str | None) -> None:
        event = data.get("hook_event_name")
        with self._lock:
            observation = self._sessions.get(data.get("session_id") or "")
        if observation is None:
            logger.debug("Claude hook %s for unobserved session ignored", event)
            return
        # Hooks fire after the model response that triggered them has fully arrived, so
        # close out that turn first to keep the audit chain in causal order.
        observation.flush_turn()
        run_id = observation.run_id

        call_id = tool_use_id or data.get("tool_use_id") or ""
        if event == "PreToolUse":
            with self._lock:
                self._tool_started[call_id] = time.monotonic()
        elif event in ("PostToolUse", "PostToolUseFailure"):
            with self._lock:
                t0 = self._tool_started.pop(call_id, None)
            failed = event == "PostToolUseFailure"
            self._recorder.tool_call(
                run_id,
                call_id,
                data.get("tool_name") or "",
                success=not failed,
                error=str(data.get("error")) if failed else None,
                duration_ms=int((time.monotonic() - t0) * 1000) if t0 is not None else 0,
            )
        elif event in ("SubagentStart", "SubagentStop"):
            self._recorder.audit(
                run_id,
                "subagent_start" if event == "SubagentStart" else "subagent_stop",
                actor=data.get("agent_type") or "subagent",
                payload={"agent_id": data.get("agent_id")},
            )
        elif event == "PreCompact":
            self._recorder.audit(
                run_id, "context_compaction", actor=HARNESS, payload={"trigger": data.get("trigger")}
            )

    def _bind(self, session_id: str, observation: _Observation) -> None:
        with self._lock:
            self._sessions[session_id] = observation

    def _unbind(self, session_id: str, observation: _Observation) -> None:
        with self._lock:
            if self._sessions.get(session_id) is observation:
                del self._sessions[session_id]


class _Observation:
    """State for one ``observe()`` call — one agent-kit run."""

    def __init__(self, observer: ClaudeAgentObserver, run_id: str, prompt: str | None) -> None:
        self._observer = observer
        self._recorder = observer._recorder
        self.run_id = run_id
        self._prompt = prompt
        self._started = False
        self._finished = False
        self._session_id: str | None = None
        # One API response can arrive as several AssistantMessages sharing a message_id;
        # accumulate them and record a single turn.
        self._turn_id: str | None = None
        self._turn_model: str | None = None
        self._turn_usage: dict[str, Any] | None = None
        self._turn_tools: list[str] = []
        self._turn_open = False

    def on_message(self, message: Any) -> None:
        try:
            self._record(message)
        except Exception:
            logger.debug("ClaudeAgentObserver failed to record %s", type(message).__name__, exc_info=True)

    def _record(self, message: Any) -> None:
        kind = type(message).__name__
        data = getattr(message, "data", None) if kind == "SystemMessage" else None
        session_id = data.get("session_id") if isinstance(data, dict) else getattr(message, "session_id", None)

        if not self._started:
            model = data.get("model") if isinstance(data, dict) else getattr(message, "model", None)
            self._recorder.start(
                self.run_id,
                model=model,
                prompt=self._prompt,
                metadata={"session_id": session_id} if session_id else None,
            )
            self._started = True
        if session_id and session_id != self._session_id:
            if self._session_id:
                self._observer._unbind(self._session_id, self)
            self._session_id = session_id
            self._observer._bind(session_id, self)

        if kind == "AssistantMessage":
            if message.message_id is None or message.message_id != self._turn_id:
                self.flush_turn()
                self._turn_id = message.message_id
                self._turn_model = message.model
                self._turn_open = True
            if message.usage:
                self._turn_usage = message.usage
            self._turn_tools.extend(
                block.name for block in message.content if type(block).__name__ == "ToolUseBlock"
            )
        elif kind == "ResultMessage":
            self.flush_turn()
            if message.is_error:
                detail = "; ".join(message.errors or []) or message.subtype
                self._recorder.error(self.run_id, "ResultError", detail)
            else:
                self._recorder.complete(
                    self.run_id, num_turns=message.num_turns, harness_cost_usd=message.total_cost_usd
                )
            self._finished = True

    def flush_turn(self) -> None:
        if not self._turn_open:
            return
        usage = self._turn_usage or {}
        self._recorder.llm_turn(
            self.run_id,
            self._turn_model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            tool_names=self._turn_tools,
        )
        self._turn_id = None
        self._turn_model = None
        self._turn_usage = None
        self._turn_tools = []
        self._turn_open = False

    def fail(self, error_type: str, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.flush_turn()
            if not self._started:
                self._recorder.start(self.run_id, model=None, prompt=self._prompt)
                self._started = True
            self._recorder.error(self.run_id, error_type, message)
        except Exception:
            logger.debug("ClaudeAgentObserver failed to record run error", exc_info=True)

    def close(self) -> None:
        if self._started and not self._finished:
            self.fail("IncompleteRun", "message stream ended without a result")
        if self._session_id:
            self._observer._unbind(self._session_id, self)
