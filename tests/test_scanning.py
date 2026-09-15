"""Tool output scanning: findings, span collection, pattern and Nullcone scanners, the policy hook."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.agent.delegation import stack_hooks
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.exceptions import RunStoppedByHookError
from agent_kit.hooks import Decision, Hooks
from agent_kit.providers.base import ProviderConfig
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
