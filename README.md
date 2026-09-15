# agent-kit

[![PyPI](https://img.shields.io/pypi/v/agent-kit-ai)](https://pypi.org/project/agent-kit-ai/)
[![Python](https://img.shields.io/pypi/pyversions/agent-kit-ai)](https://pypi.org/project/agent-kit-ai/)

**Production AI agents you can govern, afford, and prove.** Policy, cost control, security, and tamper-evident
audit that hold across every tool, MCP server, and sub-agent — with a self-hostable ops backend and
compliance-grade evidence.

```bash
pip install agent-kit-ai
```

First-party SDKs and agent frameworks give you a capable loop. agent-kit is for what comes after the demo:

```python
from agent_kit import SUSPEND, Agent, AgentConfig
from agent_kit.cloud import CloudReporter
from agent_kit.durable import SQLiteRunStore
from agent_kit.hooks import Hooks, require_approval
from agent_kit.providers import AnthropicProvider
from agent_kit.scanning import PatternScanner, scan_tool_output

agent = Agent(AnthropicProvider(), tools=[lookup_order, issue_refund, research_agent.as_tool("research", "...")],
    config=AgentConfig(
        hooks=Hooks(
            before_tool=[require_approval("issue_refund")],    # a human signs off on every refund
            after_tool=[scan_tool_output(PatternScanner())],   # poisoned tool output never reaches the model
        ),
        approver=SUSPEND,                                      # approvals can wait for days...
        run_store=SQLiteRunStore("runs.db"),                   # ...and runs survive crashes and deploys
        max_run_cost_usd=0.50,                                 # hard cap, sub-agents included
        cloud=CloudReporter(project="support", agent_name="refunds"),  # fleet metrics, alerts, hosted audit
    ),
)
```

Retry, per-provider circuit breaking, cost tracking, and a tamper-evident audit chain are on by default. Every
policy above also applies inside the `research` sub-agent.

## What you get

### Govern what agents do

- **Hooks and approval gates** — block tools, require human approval, redact tool output, or stop runs. Fail-closed; every decision is audited.
- **Tool allowlists** — enforced at call time, not just hidden from the prompt.
- **Tool output scanning** — hidden instructions, chat-template tokens, data-exfiltration links, and known-malicious URLs, domains, and hashes ([Nullcone](https://nullcone.ai) threat intel) are blocked or marked untrusted before the model reads them.
- **Agents as tools** — delegate to sub-agents that inherit the caller's policy, budget, and approvals; they can't be used to route around a rule.

### Prove what happened

- **Tamper-evident audit chain** — a hash-linked record of every model call, tool call, and policy decision; verified locally and re-verified server-side. A parent run's chain commits to each sub-agent's chain.
- **Signed evidence bundles** — Ed25519-signed exports anyone can verify offline with `agent-kit verify`, no trust in the vendor required.
- **Retention, legal holds, and deletion receipts** — tier-based retention, holds that block purges, and a signed receipt for every purged run.

### Control cost and failure

- **Cost circuit breaker** — per-run caps and daily / weekly / monthly fleet budgets stop agents before the next model call, and alert when tripped.
- **Cost per turn** — token- and cache-aware USD for current Claude and OpenAI models; unpriced models are logged, never silently $0.
- **Circuit breakers and retry** — stop hammering a failing provider; every state change lands in the audit chain.
- **Durable runs** — checkpoints at every turn; resume after a crash, suspend for approval, never run a side effect twice.

### Operate the fleet

- **Self-hostable [agent-kit Cloud](#agent-kit-cloud)** — fleet metrics, alerting (Slack, PagerDuty, webhook, SMTP), budgets, SLA context, and the hosted audit trail on your own Postgres.
- **Bring your existing agents** — Claude Agent SDK and OpenAI Agents SDK adapters, plus OTLP ingest for any OpenTelemetry-instrumented agent in any language.
- **Built for real workloads** — MCP tools, typed results, prompt caching and context management, parallel tool calls, streaming, and Anthropic / OpenAI / Ollama / any OpenAI-compatible endpoint behind one interface.

## Compliance evidence: SOC 2 and the EU AI Act

Auditors increasingly ask what your AI agents were allowed to do, what they did, and whether the record can be
trusted. agent-kit produces that evidence as a by-product of running the agent:

| What a reviewer asks | What agent-kit gives you |
|---|---|
| Who or what could act, and what was blocked? (SOC 2 CC6 — logical access) | Allowlists, policy hooks, and approval decisions, each recorded in the audit chain |
| How do you detect anomalies? (SOC 2 CC7.2 — monitoring) | Fleet metrics; circuit breaker, error-rate, cost, budget, and flagged-tool-output alerts |
| How do you evaluate and respond to incidents? (SOC 2 CC7.3–CC7.4) | Alert firings with acknowledgement history, and a per-run audit trail to investigate from |
| Can the logs be trusted and handed over? (EU AI Act record-keeping, SOC 2 evidence requests) | Hash-chained audit verified server-side; signed evidence bundles verifiable offline |
| How long is data kept, and how is it disposed of? (SOC 2 C1.2) | Retention policies, legal holds, and signed deletion receipts |

agent-kit doesn't make an organization SOC 2 or EU AI Act compliant, and agent-kit Cloud has no SOC 2 report of
its own — it gives your compliance program trustworthy records about your AI agents. Confirm the mapping with your
auditor.

Not yet: handoffs — tracked in [specs/06-harness-roadmap.md](specs/06-harness-roadmap.md).

---

## Quick start

### 8-line minimal agent

```python
import asyncio
from agent_kit import Agent
from agent_kit.providers import AnthropicProvider

async def main():
    agent = Agent(AnthropicProvider())
    result = await agent.run("Explain the Monty Hall problem in two sentences.")
    print(result.output)
    print(f"Cost: ${result.total_cost_usd:.4f}")

asyncio.run(main())
```

### Add a tool (6 more lines)

```python
import httpx
from agent_kit import tool

@tool(description="Fetch the current price of a crypto asset", idempotent=True)
async def get_price(symbol: str) -> dict:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd")).json()

agent = Agent(AnthropicProvider(), tools=[get_price])
result = await agent.run("What is the current Bitcoin price?")
```

### Production hardening in config

```python
from agent_kit import Agent, AgentConfig
from agent_kit.types import RetryPolicyConfig, CircuitBreakerConfig

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(
        system_prompt="You are a helpful assistant.",
        retry_policy=RetryPolicyConfig(max_attempts=3),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5),
        audit_enabled=True,    # tamper-evident Merkle audit chain
    ),
)
result = await agent.run("Summarize the latest AI research.")
print(f"Audit root hash: {result.audit_root_hash}")  # verify integrity later
```

### Multi-stage pipeline

```python
from agent_kit.orchestrator import LinearPipeline

pipeline = LinearPipeline([
    (researcher, "Research this topic: {input}"),
    (writer,     "Write a blog post from these facts: {input}"),
    (editor,     "Polish and tighten this draft: {input}"),
])
result = await pipeline.run("The future of AI agent frameworks")
print(result.final_output)
print(f"Total cost: ${result.total_cost_usd:.4f} across {len(result.stage_results)} stages")
```

---

## Demos

### Parallel research DAG

Three specialist agents research concurrently, then a fourth synthesizes. The DAG handles dependency resolution and runs independent nodes in parallel.

```python
# examples/research_dag.py
dag = DAGOrchestrator([
    TaskNode("market", market_agent, "Market analysis of: {input}"),
    TaskNode("tech",   tech_agent,   "Technical landscape for: {input}"),
    TaskNode("risk",   risk_agent,   "Risk assessment for: {input}"),
    TaskNode("synthesis", synthesizer,
             "Synthesize:\n{upstream:market}\n{upstream:tech}\n{upstream:risk}",
             depends_on=["market", "tech", "risk"]),
])
result = await dag.execute("autonomous AI agents in enterprise production")
```

```
$ python examples/research_dag.py
Researching: autonomous AI agents in enterprise production systems
DAG: market + tech + risk (parallel) → synthesis

============================================================
EXECUTIVE BRIEFING
============================================================
Verdict: Enterprise AI agent adoption is accelerating but premature
for mission-critical workflows without proper guardrails.

The market is projected to reach $47B by 2028 (34% CAGR), with
75% of Fortune 500 companies running pilot programs. Technically,
the shift from chain-of-thought to tool-using agents with circuit
breakers and audit trails has made production deployment viable.
However, three risks dominate: regulatory uncertainty around
autonomous decision-making, cost blowouts from retry cascades,
and the "ghost agent" problem — orphaned processes consuming
resources with no human oversight.

Recommendation: Deploy with hard cost ceilings, tamper-evident
audit logging, and circuit breakers on all provider calls.
============================================================

Execution order: market → tech → risk → synthesis
Wall time: 4.2s
Total cost: $0.0183
Total tokens: 4,271

Per-node breakdown:
  market        $0.0041    982 tokens
  tech          $0.0044  1,053 tokens
  risk          $0.0038    891 tokens
  synthesis     $0.0060  1,345 tokens
```

### Production-safe agent with audit trail

Tool allowlisting, Merkle audit chain, and compliance export. The agent has `delete_employee` registered but it's blocked — only `check_pto` and `submit_pto` are allowed.

```python
# examples/safe_agent.py
agent = Agent(
    provider=AnthropicProvider(),
    tools=[check_pto, submit_pto, delete_employee],
    config=AgentConfig(
        system_prompt="You are an HR assistant.",
        allowed_tools=["check_pto", "submit_pto"],  # delete_employee → blocked
    ),
)
result = await agent.run("I'm employee E003. Can I take 5 days off for vacation?")
```

```
$ python examples/safe_agent.py
Running agent with tool allowlist: [check_pto, submit_pto]
(delete_employee is registered but BLOCKED)

============================================================
RESPONSE
============================================================
I checked your PTO balance, Carol. You have 17 days remaining
(20 allocated, 3 used). I've submitted your request for 5 days
for your family vacation.

Your confirmation number is PTO-2026-0042.

============================================================
AUDIT TRAIL
============================================================
Events recorded: 6
Root hash: a1c9f3e7d204b85610ef38c7...
Chain integrity: VERIFIED

Cost: $0.0052 | Tokens: 1,203

Event log:
  [0] agent_start                     actor=run-8f3a2c...
  [1] llm_complete                    actor=anthropic
  [2] tool_call                       actor=check_pto
  [3] llm_complete                    actor=anthropic
  [4] tool_call                       actor=submit_pto
  [5] agent_complete                  actor=run-8f3a2c...

JSONL export: 6 records, 1,847 bytes
First record: agent_start
Last record:  agent_complete
```

### Cloud-monitored agent with live tools

Real HTTP tools (no API keys needed), circuit breaker config, console tracing, and optional cloud reporting for fleet-wide visibility.

```python
# examples/cloud_monitored.py
agent = Agent(
    provider=AnthropicProvider(),
    tools=[get_weather, top_hn_story, country_facts],
    config=AgentConfig(
        retry_policy=RetryPolicyConfig(max_attempts=3),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5, recovery_timeout_s=30),
        tracer=AgentTracer(backend="console"),
        cloud=reporter,  # optional — ships events to fleet dashboard
    ),
)
```

```
$ python examples/cloud_monitored.py
Cloud reporting: ENABLED (events → fleet dashboard)

Agent: Agent(provider='anthropic', tools=['get_weather', 'top_hn_story', 'country_facts'], model='claude-sonnet-4-20250514')
============================================================
{"span":"agent.run","kind":"agent","duration_ms":3841,"attributes":{"run_id":"e9f1..."}}
{"span":"llm.complete","kind":"llm","duration_ms":1203,"attributes":{"input_tokens":847,"output_tokens":52,"cost_usd":0.0031}}
{"tool_call":"get_weather","duration_ms":340,"success":true}
{"tool_call":"top_hn_story","duration_ms":289,"success":true}
{"tool_call":"country_facts","duration_ms":195,"success":true}
{"cost_event":true,"tokens":1847,"model":"claude-sonnet-4-20250514","usd":0.0058,"cumulative_usd":0.0089}
{"span":"llm.complete","kind":"llm","duration_ms":1814,"attributes":{"input_tokens":1203,"output_tokens":644,"cost_usd":0.0058}}

============================================================
RESPONSE
============================================================
Here's what I found:

🌤 Tokyo: 18°C (64°F), partly cloudy, 62% humidity, wind 8 mph

📰 Top HN story: "Show HN: I built a self-healing Kubernetes operator"
   by tobiaswright — 847 points

🇯🇵 Japan: Capital Tokyo, population 125,681,593, East Asia
   Languages: Japanese

============================================================
TELEMETRY
============================================================
Turns: 2
Cost:  $0.0089
Tokens: 1,847
Audit hash: f7a2e19c830d...
Trace ID: 4c91b7a3-8e2f-4d1a-b3c9-7f2e1a3d5b8c

Events shipped to agent-kit Cloud. View at your fleet dashboard.
```

---

## Providers

```python
from agent_kit.providers import AnthropicProvider

# Anthropic (default — uses ANTHROPIC_API_KEY env var)
provider = AnthropicProvider()
provider = AnthropicProvider(api_key="sk-ant-...", default_model="claude-3-haiku-20240307")

# OpenAI (requires pip install agent-kit-ai[openai])
from agent_kit.providers.openai import OpenAIProvider
provider = OpenAIProvider()

# Ollama (local models)
from agent_kit.providers.ollama import OllamaProvider
provider = OllamaProvider(default_model="llama3.2")

# Any OpenAI-compatible endpoint
from agent_kit.providers.openai import OpenAIProvider
provider = OpenAIProvider(base_url="http://localhost:11434/v1", api_key="none", default_model="mistral")
```

---

## Observability

```python
from agent_kit.observability import AgentTracer

# Zero dependencies — no-op (default)
tracer = AgentTracer()

# Structured JSON to stderr — no extra deps
tracer = AgentTracer(backend="console")

# Full OpenTelemetry (requires pip install agent-kit-ai[otel])
tracer = AgentTracer(backend="otlp", service_name="my-agent", endpoint="http://localhost:4317")

agent = Agent(provider, config=AgentConfig(tracer=tracer))
```

---

## Audit chain

Every agent run produces a tamper-evident Merkle audit chain:

```python
agent = Agent(provider, config=AgentConfig(audit_enabled=True))
result = await agent.run("Do something important.")

# Verify chain integrity
assert agent.audit.verify()
print(f"Root hash: {result.audit_root_hash}")

# Export to JSONL for compliance storage
with open("audit.jsonl", "w") as f:
    f.write(agent.audit.export_jsonl())
```

---

## Circuit breaker

The circuit breaker wraps every LLM provider call. It transitions:
- **CLOSED** → normal operation
- **OPEN** → failing fast (no LLM calls; raises `CircuitOpenError`)
- **HALF_OPEN** → probing recovery after `recovery_timeout_s`

```python
from agent_kit.types import CircuitBreakerConfig

config = AgentConfig(
    circuit_breaker=CircuitBreakerConfig(
        failure_threshold=5,      # open after 5 consecutive failures
        recovery_timeout_s=60.0,  # attempt recovery after 60s
        success_threshold=2,      # 2 successes in half-open → closed
    )
)
```

---

## Reliability

```python
from agent_kit.types import RetryPolicyConfig, BackoffConfig

policy = RetryPolicyConfig(
    max_attempts=3,
    backoff=BackoffConfig(
        initial_delay_s=1.0,
        multiplier=2.0,
        max_delay_s=30.0,
        jitter=True,
    ),
    retryable_on=["ProviderError", "httpx.TimeoutException"],
)
```

---

## Tool allowlist

Lock down which tools an agent can call — enforced at the registry level, not just advisory:

```python
agent = Agent(
    provider,
    tools=[web_search, read_file, delete_file, send_email],
    config=AgentConfig(allowed_tools=["web_search", "read_file"]),
    # delete_file and send_email raise ToolNotAllowedError if the LLM tries to call them
)
```

---

## Typed results

Pass any type Pydantic can validate — a model, dataclass, `TypedDict`, `list[...]`, enum — and get a
validated value back. Tools, hooks, budgets, and audit still apply on the way there.

```python
class Quote(BaseModel):
    customer: str
    total_usd: float

result = await agent.run("Quote Acme for 10 widgets", output_type=Quote)
result.parsed.total_usd      # validated Quote; result.output is the raw JSON
```

- Anthropic and OpenAI constrain the answer with native structured outputs. Ollama does too, except in
  runs with tools, where its grammar would block tool calls — there the schema goes in the system prompt
  and repairs are constrained natively. Other providers get the schema in the system prompt.
- An answer that fails validation goes back to the model with the errors (`AgentConfig(output_retries=2)`),
  then `OutputValidationError` is raised. Each failure is audited as `output_validation_failed`.
- `agent.stream(prompt, output_type=Quote)` streams the raw JSON; `agent.last_result.parsed` holds the value.

Full example: [`examples/typed_output.py`](examples/typed_output.py).

---

## Context management

Long tool-heavy runs stay correct, cached, and inside the context window with no configuration:

- **Prompt caching is on.** Anthropic requests carry a breakpoint on the system prompt plus automatic
  caching of the growing conversation; cached input shows up in `turn.cost.cache_read_tokens`.
  `AgentConfig(prompt_caching=False)` turns it off.
- **Thinking and compaction blocks round-trip verbatim.** Claude models that think (Claude Opus 5 and
  Sonnet 5 do by default) get every thinking block back unchanged, including through `SQLiteMemory`.
- **History is trimmed by tokens, not message count.** When the next request would exceed
  `context_budget_tokens` (default 150K), the oldest turns are cut once down to half the budget, never
  separating a tool call from its result. Between cuts the history only grows at the end, so the prompt
  prefix — and the cache — stays valid. Each cut is audited as `context_trimmed`.

```python
config = AgentConfig(
    effort="high",                    # Anthropic output_config.effort / OpenAI reasoning_effort
    thinking="adaptive",              # Anthropic thinking
    provider_options={"thinking": {"display": "summarized"}},   # merged into every request
    compaction=Compaction(trigger_tokens=100_000),              # Anthropic server-side summarisation
    clear_tool_results=ClearToolResults(trigger_tokens=60_000, keep=4),
)
```

With `compaction`, the API summarises earlier context itself: client trimming stops, compaction cost is
included in `total_cost_usd`, and each summarisation is audited as `context_compacted` (tool-result
clearing as `context_edited`). `memory_window=N` still caps history by message count if you want it.
Full example: [`examples/long_running_agent.py`](examples/long_running_agent.py).

---

## Hooks and approval gates

Put policy around what an agent does — deny tools, require a human, redact what tools return, or stop the run:

```python
from agent_kit.hooks import Decision, Hooks, deny_tools, require_approval

async def approve(req):                      # Slack button, approvals API, terminal prompt…
    return await slack.ask(f"Run {req.tool_name}({req.arguments})?")

agent = Agent(
    provider,
    tools=[lookup_order, refund_order, close_account],
    config=AgentConfig(
        hooks=Hooks(
            before_tool=[
                deny_tools("close_account", reason="needs a human", stop_run=True),
                require_approval("refund_order", reason="refunds move money"),
            ],
            after_tool=[redact_cards],        # return Decision.replace(masked_output)
            before_llm=[stop_after_hours],    # return Decision.deny("outside business hours")
        ),
        approver=approve,
        approval_timeout_s=300,              # no answer → deny
    ),
)
```

| Hook | Can return |
|---|---|
| `before_tool` | `allow` · `deny(reason, stop_run=False)` · `ask(reason)` |
| `after_tool` | `allow` · `replace(output)` · `deny(reason)` |
| `before_llm` | `allow` · `deny(reason)` (stops the run) |

Hooks can be sync or async; returning `None` allows. **Everything else fails closed:** a hook that raises,
an `ask` with no approver, a denied or timed-out approval — all deny. A denied tool call reaches the model
as a tool error it can work around; `stop_run=True` raises `RunStoppedByHookError`. Every decision is an
audit event (`tool_denied`, `approval_requested`, `approval_granted`, `approval_denied`,
`tool_output_replaced`, `llm_call_denied`). Full example: [`examples/approval_gate.py`](examples/approval_gate.py).

---

## Durable runs

Give an agent a run store and every run checkpoints at each turn boundary. Runs survive crashes and deploys,
and approvals can wait for a human for as long as it takes:

```python
from agent_kit import SUSPEND
from agent_kit.durable import SQLiteRunStore

agent = Agent(provider, tools=[refund], config=AgentConfig(
    run_store=SQLiteRunStore("runs.db"),
    hooks=Hooks(before_tool=[require_approval("refund")]),
    approver=SUSPEND,                                  # park the run instead of waiting inline
))
result = await agent.run("Refund order A-1001", run_id="ticket-9913")
result.status               # "suspended"
result.pending_approvals    # [PendingApproval(call_id=..., tool_name="refund", arguments={...})]

# hours later, in any process that builds the same Agent:
result = await agent.resume("ticket-9913", approvals={call_id: True})
```

- **Crash recovery.** `agent.resume(run_id)` continues a run that crashed, failed, or was killed, from its
  last checkpoint. A tool that was running when the process died is re-run only if it is marked
  `idempotent=True`; otherwise the model is told the call was interrupted, so a refund never runs twice.
- **One owner at a time.** Checkpoint writes are compare-and-swap: if two workers resume the same run, one
  gets `RunConflictError` before it can execute anything.
- **Continuity.** Memory, turns, run cost, typed-output state, and the audit chain are restored — the chain
  verifies across the suspension (`run_suspended`, `run_resumed`, `tool_interrupted` events).
- `resume_stream()` streams a resumed run; resuming a completed run returns its stored result.

`RunStore` is a small async protocol (`save` / `load` / `mark_tool_started` / `list` / `delete`), so Postgres
or Redis stores drop in. Full example: [`examples/durable_approval.py`](examples/durable_approval.py).

---

## Agents as tools

Turn any agent into a tool another agent can delegate to:

```python
research = Agent(provider, tools=[lookup_order]).as_tool("research", "Look up facts about orders.")
refunds = Agent(provider, tools=[issue_refund], config=AgentConfig(
    hooks=Hooks(before_tool=[require_approval("issue_refund")]),
)).as_tool("refunds", "Handle a refund request.", output_type=RefundOutcome)

lead = Agent(provider, tools=[research, refunds], config=AgentConfig(
    run_store=SQLiteRunStore("runs.db"),
    approver=SUSPEND,
    hooks=Hooks(before_tool=[deny_tools("close_account", reason="manual only")]),
    max_run_cost_usd=1.00,
))
```

The model calls `refunds` with a `task`; every call is a fresh child run with its own memory and audit chain,
so the same agent tool can run several times in one turn. Delegation cannot escape the parent run:

| The child keeps | The child inherits from the calling run |
|---|---|
| provider, model, tools, `allowed_tools`, system prompt, turn limit, retry, circuit breaker, context settings | hooks (run **after** the child's own — any deny wins), approver, run store, and whatever is left of `max_run_cost_usd` |

- **Approvals bubble up.** When `issue_refund` asks for approval inside `refunds`, the lead run suspends and
  `result.pending_approvals` lists it as `"<refunds call>/<issue_refund call>"`.
  `lead.resume(run_id, approvals={that_id: True})` resumes the child and then the lead.
- **One budget.** Child spend counts toward the lead's `max_run_cost_usd` before its next model call and is
  included in `total_cost_usd`.
- **Linked audit.** The lead's `tool_call` event records the child's `delegated_run_id` and final
  `delegated_root_hash`, so tampering with a child chain shows from the parent. Child runs report to
  agent-kit Cloud as their own runs with `parent_run_id`.
- **Crash-safe.** A delegation interrupted by a crash resumes the child from its checkpoint.
- Parent policies match tool names, and they see the child's tools: `allow_only(...)` on a lead must list the
  child tools it permits, too. Nesting is limited by `AgentConfig(max_delegation_depth=5)`.

For a fixed graph of agents, use `DAGOrchestrator`. Full example: [`examples/delegation.py`](examples/delegation.py).

---

## Tool output scanning

Everything a tool returns — a web page, a document, an MCP result, a delegated agent's answer — lands in the
model's context as if it were trustworthy. Screen it first:

```python
from agent_kit.scanning import NullconeScanner, PatternScanner, scan_tool_output

agent = Agent(provider, tools=[fetch_page, research], config=AgentConfig(hooks=Hooks(after_tool=[
    scan_tool_output(
        PatternScanner(),                      # local rules, no network
        NullconeScanner(),                     # optional threat-intel lookups
        block_at="high", warn_at="medium",     # stop_run_at=... is also available
        trusted_tools=["lookup_order"],        # internal tools you don't need to screen
    ),
])))
```

The most severe finding decides what the model sees:

| Severity reached | Model sees |
|---|---|
| `stop_run_at` (off by default) | nothing — the run stops with `RunStoppedByHookError` |
| `block_at` (default `high`) | `Tool output blocked: possible prompt injection: <rule> (<severity>)` |
| `warn_at` (default `medium`) | the output inside an `agentkit_scan` envelope that marks it untrusted data |
| below `warn_at` | the output unchanged; the finding is still recorded |

`PatternScanner` rules (disable any with `disable=[...]`, add your own with `extra_rules=[PatternRule(...)]`):

| Rule | Severity | Catches |
|---|---|---|
| `unicode_tags` | critical | instructions hidden in invisible Unicode tag characters |
| `role_token` | critical | chat-template control tokens and fake system / tool-result tags |
| `instruction_override` | high | text telling the model to discard its previous instructions or system prompt |
| `hidden_text` | high | bidirectional overrides and runs of zero-width characters |
| `encoded_payload` | high | base64 blobs that decode to either of the two rules above |
| `exfil_markdown` | medium | markdown images and links that carry data out in their query string |
| `persona_switch` | medium | named jailbreak personas and "developer mode" switches |

Rules favour precision: tool output is full of ordinary prose, docs, and code, so everyday phrasing isn't flagged.

`NullconeScanner` checks URLs, domains, IPs, and hashes found in the output against the
[Nullcone](https://nullcone.ai) threat database. It sends those indicators — never the output, never URL query
strings or credentials — so treat it as data egress. Confidence filtering comes from the API (`unverified` and
low-score hits are ignored by default); reserved names and private IPs are never looked up; answers are cached; it
fails open on errors unless `fail_closed=True`, and pauses after HTTP 429.

Every decision with findings appends a `tool_output_flagged` audit event and reports it to agent-kit Cloud (rule,
severity, JSON path, and matched indicator — no output text), where a `tool_output_flagged` alert rule with
`min_severity` pages on-call. A lead agent's scanner also screens every delegated child's tool outputs. Any object
with a `name` and an `async scan(spans) -> list[Finding]` method is a scanner. Full example:
[`examples/scanned_tools.py`](examples/scanned_tools.py); design: [`specs/17-tool-output-scanning.md`](specs/17-tool-output-scanning.md).

---

## MCP tools

Use tools from any [Model Context Protocol](https://modelcontextprotocol.io) server — `pip install agent-kit-ai[mcp]`:

```python
from agent_kit.tools.mcp import MCPToolset, http, require_approval_unless_read_only, stdio

async with MCPToolset(
    stdio("fs", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/srv/docs"),
    http("linear", "https://mcp.linear.app/mcp", headers={"Authorization": f"Bearer {key}"}),
) as mcp:
    agent = Agent(provider, tools=[*mcp.tools, my_tool], config=AgentConfig(
        hooks=Hooks(before_tool=[require_approval_unless_read_only(mcp)]),
        approver=approve,
    ))
    await agent.run("Summarise the onboarding docs and file a Linear issue for anything out of date")
# every server disconnected (subprocesses terminated) here
```

- MCP tools are ordinary agent-kit tools named `server__tool`: `allowed_tools`, hooks, approvals, budgets, audit, and Cloud reporting all apply.
- Structured results come back as data; text, images, and resources are summarised; tool errors and timeouts (`call_timeout_s`) reach the model as tool errors.
- `require_approval_unless_read_only(mcp)` asks before any MCP tool not marked `readOnlyHint` — a server can't skip the gate by omitting hints.
- A `required` server that fails to connect closes the others and raises `MCPConnectionError`; `required=False` skips it.

Full example: [`examples/mcp_tools.py`](examples/mcp_tools.py).

---

## Installation

```bash
# Core (Anthropic only)
pip install agent-kit-ai

# With OpenAI support
pip install agent-kit-ai[openai]

# With OpenTelemetry
pip install agent-kit-ai[otel]

# Everything
pip install agent-kit-ai[all]
```

---

## agent-kit Cloud

Connect any agent to the **agent-kit Cloud** backend — a hosted service that gives you a fleet dashboard, audit trail storage, alerting, and SLA-backed support without running any infrastructure yourself.

```python
from agent_kit.cloud import CloudReporter

reporter = CloudReporter(
    api_key="akt_live_...",      # or set AGENTKIT_API_KEY env var
    project="production",
    agent_name="billing-assistant",
)

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(cloud=reporter),
)
result = await agent.run("Process this invoice.")
# Events are batched and shipped automatically — no await needed.
```

`CloudReporter` is **fire-and-forget**: network errors are logged at `DEBUG` level and never propagate to your agent. Performance is completely unaffected by cloud connectivity.

### What gets reported

| Event | When |
|---|---|
| `run_start` | Agent loop begins |
| `turn_complete` | Each LLM response + tool calls |
| `run_complete` | Successful finish (includes audit root hash) |
| `run_error` | Unhandled exception |
| `circuit_state_change` | CB opens / half-opens / closes |
| `audit_flush` | Full Merkle chain (if `audit_enabled=True`) |

### CloudReporter options

```python
CloudReporter(
    api_key="akt_live_...",
    project="production",
    agent_name="billing-assistant",
    flush_interval_s=5.0,    # how often to batch-send (default 5s)
    max_queue_size=1000,     # drop events if queue exceeds this
    include_output=False,    # never ships LLM output text to cloud
)
```

See [`docs/cloud-quickstart.md`](docs/cloud-quickstart.md) to get started, or [`docs/self-hosting.md`](docs/self-hosting.md) to run the backend yourself.

### Stop runaway spend

```python
reporter = CloudReporter(project="support", agent_name="support-bot")

agent = Agent(
    AnthropicProvider(),
    config=AgentConfig(
        cloud=reporter,
        max_run_cost_usd=2.00,   # per run, enforced locally
        enforce_budgets=True,    # fleet budgets defined in agent-kit Cloud
    ),
)
```

Budgets (`$200/day for support-bot`) live in agent-kit Cloud at `/v1/budgets`; when one trips, the next
model call raises `BudgetExceededError` and a `budget_exceeded` alert fires. See
[`docs/cloud-quickstart.md`](docs/cloud-quickstart.md#stop-runaway-spend).

### Already on another harness?

Keep it. Adapters report Claude Agent SDK and OpenAI Agents SDK runs — turns, tool calls,
subagents, handoffs, guardrails, cost — to the same audit trail, fleet dashboard, and alerts. The
audit chain is built on your machine, and nothing changes on the server:

```python
# Claude Agent SDK — pip install agent-kit-ai[claude-agent-sdk]
from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver

observer = ClaudeAgentObserver(CloudReporter(project="support"))
options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Grep"]))
async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
    ...  # messages arrive unchanged

# OpenAI Agents SDK — pip install agent-kit-ai[openai-agents]
from agent_kit.integrations.openai_agents import AgentKitTraceProcessor

add_trace_processor(AgentKitTraceProcessor(CloudReporter(project="support")))
```

Runnable versions: [`examples/claude_agent_sdk_monitored.py`](examples/claude_agent_sdk_monitored.py),
[`examples/openai_agents_monitored.py`](examples/openai_agents_monitored.py).

---

## License

[Rising Sun License v1.0](LICENSE). Free for personal use. Commercial deployments connect to the Nous network.
