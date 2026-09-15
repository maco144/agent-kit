"""Tool output scanning: findings, span collection, pattern and Nullcone scanners, the policy hook."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.agent.delegation import child_run_id, stack_hooks
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.durable import SQLiteRunStore
from agent_kit.exceptions import RunStoppedByHookError
from agent_kit.hooks import Decision, Hooks, ToolResultContext, require_approval
from agent_kit.providers.base import ProviderConfig
from agent_kit.scanning import ENVELOPE_KEY, TextSpan, collect_spans, scan_tool_output
from agent_kit.scanning.policy import NOTICE
from agent_kit.types import CostSummary, Finding, Message, RetryPolicyConfig, ToolCall, Turn


class Scripted:
    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn | BaseException) -> None:
        self.turns = list(turns)
        self.requests: list[list[Message]] = []

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append(list(messages))
        item = self.turns.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        turn = await self.complete(messages, **kw)
        if turn.message_out and turn.message_out.content:
            yield turn.message_out.content
        yield turn


def calls(*specs: tuple[str, dict[str, Any]]) -> Turn:
    tcs = [ToolCall(tool_name=n, arguments=a, call_id=f"{n}-{i}") for i, (n, a) in enumerate(specs)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tcs), tool_calls=tcs,
                cost=CostSummary(total_tokens=10, cost_usd=0.01))


def final(text: str = "done") -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary(total_tokens=5, cost_usd=0.01))


NO_RETRY = RetryPolicyConfig(max_attempts=1)
PAGES: dict[str, str] = {}
fetched: list[str] = []


@tool(description="fetch a web page")
async def fetch_page(url: str) -> dict[str, Any]:
    fetched.append(url)
    return {"url": url, "body": PAGES[url]}


@pytest.fixture(autouse=True)
def _reset():
    PAGES.clear()
    fetched.clear()


def finding(rule: str = "rule_a", severity: str = "low", location: str = "$.body", indicator: str | None = None,
            scanner: str = "fake") -> Finding:
    return Finding(scanner=scanner, rule=rule, severity=severity, message="test finding", location=location,
                   indicator=indicator)


def tool_messages(provider: Scripted) -> list[Message]:
    return [m for m in provider.requests[-1] if m.role == "tool"]


def audit_spy(agent: Agent) -> list[tuple[str, dict[str, Any]]]:
    assert agent.audit is not None
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return recorded


def recording_reporter(agent_name: str = "lead") -> tuple[CloudReporter, list[CloudEvent]]:
    reporter = CloudReporter(api_key="akt_test", project="proj", agent_name=agent_name)
    events: list[CloudEvent] = []

    async def enqueue(event: CloudEvent) -> None:
        events.append(event)

    reporter._enqueue = enqueue  # type: ignore[method-assign]
    return reporter, events


# --- Findings on decisions ---------------------------------------------------------------------------


def test_decision_findings_default_and_factories():
    f = finding()
    assert Decision.allow().findings == ()
    assert Decision.allow(findings=[f]).findings == (f,)
    assert Decision.deny("no", stop_run=True, findings=[f]) == Decision("deny", reason="no", stop_run=True, findings=(f,))
    assert Decision.replace({"x": 1}, "why", findings=[f]).findings == (f,)
    assert Decision.ask("check").findings == ()


def build(decision: Decision, **config: Any) -> tuple[Agent, Scripted, list[tuple[str, dict[str, Any]]]]:
    provider = Scripted(calls(("fetch_page", {"url": "/a"})), final())
    agent = Agent(provider, tools=[fetch_page], config=AgentConfig(
        hooks=Hooks(after_tool=[lambda ctx: decision]), retry_policy=NO_RETRY, **config))
    return agent, provider, audit_spy(agent)


async def test_flagged_decision_is_audited_and_reported_without_content():
    PAGES["/a"] = "private page body"
    reporter, events = recording_reporter()
    decision = Decision.allow(findings=[finding("rule_a", "low"), finding("rule_b", "medium", indicator="evil.net")])
    agent, provider, recorded = build(decision, cloud=reporter)

    result = await agent.run("go")

    (payload,) = [p for event, p in recorded if event == "tool_output_flagged"]
    assert payload == {
        "call_id": "fetch_page-0",
        "tool_name": "fetch_page",
        "action": "allowed",
        "max_severity": "medium",
        "findings": [
            {"scanner": "fake", "rule": "rule_a", "severity": "low", "location": "$.body", "indicator": None},
            {"scanner": "fake", "rule": "rule_b", "severity": "medium", "location": "$.body", "indicator": "evil.net"},
        ],
    }
    assert "private page body" not in json.dumps(payload)
    (event,) = [e for e in events if e.event_type.value == "tool_output_flagged"]
    assert (event.run_id, event.payload) == (result.run_id, payload)
    assert tool_messages(provider)[0].content == '{"url": "/a", "body": "private page body"}'


@pytest.mark.parametrize(("decision", "action", "follow_up"), [
    (Decision.deny("bad", findings=[finding(severity="high")]), "blocked", "tool_denied"),
    (Decision.replace({"wrapped": True}, "careful", findings=[finding(severity="medium")]), "wrapped", "tool_output_replaced"),
])
async def test_flag_action_matches_the_decision(decision, action, follow_up):
    PAGES["/a"] = "page"
    agent, _, recorded = build(decision)
    await agent.run("go")
    types = [event for event, _ in recorded]
    assert types.index("tool_output_flagged") < types.index(follow_up)
    (payload,) = [p for event, p in recorded if event == "tool_output_flagged"]
    assert payload["action"] == action


async def test_stop_run_decision_is_recorded_as_stopped():
    PAGES["/a"] = "page"
    agent, _, recorded = build(Decision.deny("stop", stop_run=True, findings=[finding(severity="critical")]))
    with pytest.raises(RunStoppedByHookError):
        await agent.run("go")
    (payload,) = [p for event, p in recorded if event == "tool_output_flagged"]
    assert (payload["action"], payload["max_severity"]) == ("stopped", "critical")


async def test_decisions_without_findings_record_nothing():
    PAGES["/a"] = "page"
    agent, _, recorded = build(Decision.deny("plain deny"))
    await agent.run("go")
    assert "tool_output_flagged" not in [event for event, _ in recorded]


def test_stack_hooks_runs_a_shared_hook_once():
    def shared(ctx: Any) -> None: ...
    def child_only(ctx: Any) -> None: ...
    def parent_only(ctx: Any) -> None: ...

    stacked = stack_hooks(Hooks(after_tool=[child_only, shared]), Hooks(after_tool=[shared, parent_only]))
    assert stacked is not None and stacked.after_tool == [child_only, shared, parent_only]


# --- Span collection ---------------------------------------------------------------------------------


def spans_of(output: Any, error: str | None = None, max_chars: int = 200_000) -> list[tuple[str, str]]:
    return [(s.path, s.text) for s in collect_spans(output, error, max_chars)]


def test_collect_spans_paths_keys_and_error():
    output = {"title": "a", "items": [{"snippet": "b"}, "c", 7, None], "odd key": "d", 3: "e", "empty": ""}
    assert spans_of(output, "boom") == [
        ("$.title#key", "title"), ("$.title", "a"),
        ("$.items#key", "items"), ("$.items[0].snippet#key", "snippet"), ("$.items[0].snippet", "b"),
        ("$.items[1]", "c"),
        ('$["odd key"]#key', "odd key"), ('$["odd key"]', "d"),
        ('$["3"]', "e"),
        ("$.empty#key", "empty"),
        ("$error", "boom"),
    ]
    assert spans_of("plain") == [("$", "plain")]
    assert spans_of(None) == []


def test_collect_spans_skips_envelopes_and_stops_at_max_chars():
    wrapped = {"x": {ENVELOPE_KEY: {"severity": "medium"}, "untrusted_content": "zzz"}, "y": "keep"}
    assert spans_of(wrapped) == [("$.x#key", "x"), ("$.y#key", "y"), ("$.y", "keep")]
    assert spans_of("abcdef", max_chars=4) == [("$", "abcd")]
    assert spans_of({"a": "xy", "b": "zz"}, "err", max_chars=5) == [("$.a#key", "a"), ("$.a", "xy"), ("$.b#key", "b"), ("$.b", "z")]


# --- scan_tool_output ---------------------------------------------------------------------------------


class MarkerScanner:
    """Flags every span containing ``marker``; records the spans it saw."""

    def __init__(self, marker: str = "FLAG", severity: str = "high", rule: str = "marker", name: str = "marker",
                 error: Exception | None = None) -> None:
        self.name, self.marker, self.severity, self.rule, self.error = name, marker, severity, rule, error
        self.seen: list[TextSpan] = []

    async def scan(self, spans: Any) -> list[Finding]:
        self.seen.extend(spans)
        if self.error is not None:
            raise self.error
        return [Finding(scanner=self.name, rule=self.rule, severity=self.severity, message="marker found",
                        location=s.path) for s in spans if self.marker in s.text]


def result_ctx(output: Any, error: str | None = None, tool_name: str = "fetch_page") -> ToolResultContext:
    return ToolResultContext(run_id="r1", turn=1, tool_name=tool_name, arguments={}, call_id="c1",
                             output=output, error=error)


@pytest.mark.parametrize(("severity", "options", "kind", "stop_run"), [
    ("low", {}, "allow", False),
    ("medium", {}, "replace", False),
    ("high", {}, "deny", False),
    ("critical", {}, "deny", False),
    ("critical", {"stop_run_at": "critical"}, "deny", True),
    ("high", {"block_at": None, "warn_at": None}, "allow", False),
    ("high", {"block_at": "critical"}, "replace", False),
])
async def test_decision_tiers(severity, options, kind, stop_run):
    hook = scan_tool_output(MarkerScanner(severity=severity), **options)
    decision = await hook(result_ctx({"body": "FLAG here"}))
    assert decision is not None
    assert (decision.kind, decision.stop_run) == (kind, stop_run)
    assert [(f.rule, f.severity, f.location) for f in decision.findings] == [("marker", severity, "$.body")]
    expected_reason = None if kind == "allow" else f"possible prompt injection: marker ({severity})"
    assert decision.reason == expected_reason
    if kind == "replace":
        assert decision.output == {
            ENVELOPE_KEY: {"severity": severity, "rules": ["marker"], "notice": NOTICE},
            "untrusted_content": {"body": "FLAG here"},
        }


async def test_no_findings_trusted_tools_and_empty_output_return_none():
    scanner = MarkerScanner()
    assert await scan_tool_output(scanner)(result_ctx({"body": "clean"})) is None
    assert await scan_tool_output(scanner, trusted_tools=["lookup"])(result_ctx("FLAG", tool_name="lookup")) is None
    assert await scan_tool_output(scanner)(result_ctx(None)) is None
    assert [s.text for s in scanner.seen] == ["body", "clean"]


async def test_error_only_warning_is_blocked():
    decision = await scan_tool_output(MarkerScanner(severity="medium"))(result_ctx(None, error="FLAG in error"))
    assert decision is not None and decision.kind == "deny"
    assert [f.location for f in decision.findings] == ["$error"]


async def test_reason_names_the_top_finding_and_counts_the_rest():
    hook = scan_tool_output(MarkerScanner(severity="medium", rule="soft"), MarkerScanner(severity="high", rule="hard"))
    decision = await hook(result_ctx({"a": "FLAG", "b": "FLAG"}))
    assert decision is not None
    assert decision.reason == "possible prompt injection: hard (high) and 3 more"
    assert [f.rule for f in decision.findings] == ["soft", "soft", "hard", "hard"]


def test_policy_validation():
    with pytest.raises(ValueError, match="at least one scanner"):
        scan_tool_output()
    with pytest.raises(ValueError, match="thresholds must be ordered"):
        scan_tool_output(MarkerScanner(), warn_at="high", block_at="medium")
    with pytest.raises(ValueError, match="thresholds must be ordered"):
        scan_tool_output(MarkerScanner(), block_at="critical", stop_run_at="high")


def scanned_agent(provider: Scripted, scanner: Any, tools: list[Any] | None = None, **options: Any) -> Agent:
    return Agent(provider, tools=tools or [fetch_page], config=AgentConfig(
        hooks=Hooks(after_tool=[scan_tool_output(scanner, **options)]), retry_policy=NO_RETRY))


async def test_wrapped_output_reaches_the_model_as_the_envelope():
    PAGES["/a"] = "FLAG text"
    provider = Scripted(calls(("fetch_page", {"url": "/a"})), final())
    await scanned_agent(provider, MarkerScanner(severity="medium")).run("go")
    assert json.loads(tool_messages(provider)[0].content) == {
        ENVELOPE_KEY: {"severity": "medium", "rules": ["marker"], "notice": NOTICE},
        "untrusted_content": {"url": "/a", "body": "FLAG text"},
    }


async def test_blocked_output_never_reaches_the_model():
    PAGES["/a"] = "FLAG text"
    provider = Scripted(calls(("fetch_page", {"url": "/a"})), final())
    await scanned_agent(provider, MarkerScanner(severity="high")).run("go")
    assert tool_messages(provider)[0].content == "Error: Tool output blocked: possible prompt injection: marker (high)"
    assert not any("FLAG text" in (m.content or "") for request in provider.requests for m in request)


async def test_stop_run_at_stops_the_run():
    PAGES["/a"] = "FLAG text"
    provider = Scripted(calls(("fetch_page", {"url": "/a"})), final())
    with pytest.raises(RunStoppedByHookError):
        await scanned_agent(provider, MarkerScanner(severity="critical"), stop_run_at="critical").run("go")


async def test_raising_scanner_fails_closed():
    PAGES["/a"] = "anything"
    provider = Scripted(calls(("fetch_page", {"url": "/a"})), final())
    await scanned_agent(provider, MarkerScanner(error=RuntimeError("scanner down"))).run("go")
    assert tool_messages(provider)[0].content == (
        "Error: Tool output blocked: hook error: RuntimeError: scanner down"
    )


async def test_lead_scanner_screens_child_tool_output_in_the_child_run():
    PAGES["/a"] = "FLAG text"
    reporter, events = recording_reporter()
    child_provider = Scripted(calls(("fetch_page", {"url": "/a"})), final("nothing useful found"))
    research = Agent(child_provider, tools=[fetch_page], config=AgentConfig(retry_policy=NO_RETRY)).as_tool(
        "research", "Investigate.")
    lead = Agent(Scripted(calls(("research", {"task": "look"})), final("ok")), tools=[research], config=AgentConfig(
        hooks=Hooks(after_tool=[scan_tool_output(MarkerScanner())]), cloud=reporter, retry_policy=NO_RETRY))

    result = await lead.run("go")

    assert result.run_id is not None
    assert tool_messages(child_provider)[0].content.startswith("Error: Tool output blocked")
    flagged = [e for e in events if e.event_type.value == "tool_output_flagged"]
    assert [(e.run_id, e.payload["tool_name"]) for e in flagged] == [(child_run_id(result.run_id, "research-0"), "fetch_page")]


async def test_durable_resume_does_not_rescan(tmp_path):
    PAGES["/a"] = "page text"

    @tool(description="refund an order")
    async def refund(order_id: str) -> dict[str, Any]:
        return {"refunded": order_id}

    scanner = MarkerScanner()

    def lead(provider: Scripted) -> Agent:
        return Agent(provider, tools=[fetch_page, refund], config=AgentConfig(
            run_store=SQLiteRunStore(tmp_path / "runs.db"), approver=SUSPEND, retry_policy=NO_RETRY,
            hooks=Hooks(before_tool=[require_approval("refund")], after_tool=[scan_tool_output(scanner)])))

    suspended = await lead(Scripted(calls(("fetch_page", {"url": "/a"}), ("refund", {"order_id": "1"})))).run(
        "go", run_id="t1")
    assert suspended.status == "suspended"
    await lead(Scripted(final())).resume("t1", approvals={"refund-1": True})
    assert [s.text for s in scanner.seen].count("page text") == 1
