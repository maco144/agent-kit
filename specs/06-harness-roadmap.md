# Spec 06 — Harness Roadmap: Fundamentals, Parity, Wedge

Status: **active** · Written 2026-09-13 · Owner: Alex

agent-kit's reliability and ops layer (circuit breakers, tamper-evident audit, self-hosted fleet
metrics and alerting) is differentiated. The agent loop underneath it has fallen behind what
first-party harnesses (Claude Agent SDK, OpenAI Agents SDK) and frameworks (LangGraph, Pydantic AI)
now ship as table stakes, and two core paths were broken as of this writing. This spec is the
working plan to close that gap, in order.

## Where we stand (2026-09-13, updated after Tier 1)

| Capability | agent-kit | First-party SDKs | LangGraph / Pydantic AI |
|---|---|---|---|
| Multi-turn tool loop | ✅ (was broken — fixed in Tier 1) | ✅ | ✅ |
| Parallel tool calls | ✅ (Tier 1) | ✅ | ✅ |
| MCP client | ✅ (2.1) | ✅ | ✅ |
| Typed / structured output | ✅ (2.2) | ✅ | ✅ |
| Approval gates / hooks | ✅ (2.3) | ✅ | ✅ |
| Context management | ✅ (2.4) | ✅ | Partial |
| Durable runs / resume | ✅ (2.5) | Sessions | ✅ |
| Sub-agents / handoffs | Delegation ✅ (2.6); no handoffs | ✅ | ✅ |
| Streaming with tools | ✅ (Tier 1) | ✅ | ✅ |
| Per-provider circuit breaker | ✅ | ❌ | ❌ |
| Tamper-evident audit + server verify | ✅ | ❌ | ❌ |
| Tool-output injection scanning | ✅ (3.4) | Partial | ❌ |
| Self-hosted fleet metrics + alerting | ✅ | ❌ | Hosted, paid |
| Provider-neutral | ✅ | Vendor-bound | ✅ |

## Defects found during the review

1. Assistant tool calls are not stored on `Message`, so the second provider request carries an empty
   assistant turn and orphaned tool results — rejected by both the Anthropic and OpenAI APIs.
2. SDK tests mock the provider class, so adapter request shapes are never exercised; respx also no
   longer intercepts `anthropic>=1.0`, which uses `httpx2`.
3. Cost table: Claude Opus 5 / Sonnet 5 / Fable report $0; Opus 4.5–4.8 bill at 3× actual;
   Haiku 4.5 understated; cache tokens ignored; `gpt-4o-mini` priced as `gpt-4o`.
4. `Agent.stream()` bypasses the loop — no tools, retry, circuit breaker, audit, cloud events — and
   never records the assistant reply.
5. Memory windows trim by message count and can split a tool call from its results.
6. README comparison table makes competitor claims that no longer hold.

## Tier 1 — Make the fundamentals true

Implementation plan: `docs/superpowers/plans/2026-09-13-tier1-harness-fundamentals.md`

- [x] **1.1** Tool calls round-trip through history (Anthropic, OpenAI/Ollama, SQLite persistence)
- [x] **1.2** Provider request-capture tests replace class-level mocks for adapter coverage
- [x] **1.3** Memory windows never orphan tool results
- [x] **1.4** Parallel tool execution; sync tools off the event loop
- [x] **1.5** Pricing: exact model families, longest-prefix match, cache tokens, warn on unknown
- [x] **1.6** `Agent.stream()` runs the full loop with tools, retry, circuit breaker, audit, cloud
- [x] **1.7** README positioning and contributor testing guidance rewritten to match reality

## Tier 2 — Parity

- [x] **2.1 MCP client** — design: `specs/12-mcp-client.md`. load tools from MCP servers (stdio + streamable HTTP) into `ToolRegistry`;
      allowlist enforcement applies unchanged.
- [x] **2.2 Typed results** — design: `specs/13-typed-results.md`. `agent.run(prompt, output_type=Model)` returns a validated Pydantic
      instance via provider structured outputs; fall back to schema-in-prompt + validation.
- [x] **2.3 Hooks + approval gates** — design: `specs/11-hooks-approval-gates.md`. `before_tool` / `after_tool` / `before_llm` hooks returning
      allow / deny / ask; `ask` pauses the run for an external decision. Extends `ToolRegistry`.
- [x] **2.4 Context management** — design: `specs/14-context-management.md`. prompt-caching breakpoints, compaction/context-editing passthrough,
      thinking and effort settings on `AgentConfig`, token-based windowing.
- [x] **2.5 Durable runs** — design: `specs/15-durable-runs.md`. checkpoint loop state per `run_id` (SQLite); `agent.resume(run_id)`.
- [x] **2.6 Agent-as-tool** — design: `specs/16-agent-as-tool.md`. expose an `Agent` as a `Tool` for delegation; DAG stays for static graphs.

## Tier 3 — Wedge (what only we offer)

- [x] **3.1a Harness adapters** — Claude Agent SDK and OpenAI Agents SDK runs report to Cloud with a
      client-side audit chain and no server change (`specs/07-harness-adapters.md`).
- [x] **3.1b OTLP ingest** — design: `specs/08-otlp-ingest.md`. Accept OpenTelemetry GenAI spans so harnesses in any language (TypeScript
      included) reach audit, fleet metrics, alerts, and SLA context; chain built at ingest.
- [x] **3.2 Cost circuit breaker** — design: `specs/09-cost-circuit-breaker.md`. per-run and per-org dollar ceilings that trip like the failure
      breaker and fire an alert. Reuses the breaker, cost tracking, and alerting already built.
- [x] **3.3 Compliance exports** — design: `specs/10-compliance-exports.md`. package the hash-chained audit and server-side verification as
      signed exports with retention policies for regulated buyers (EU AI Act record-keeping,
      SOC 2 evidence).
- [x] **3.4 Tool-output injection scanning** — design: `specs/17-tool-output-scanning.md`. an `after_tool` policy that screens tool results for
      prompt-injection payloads before they re-enter context (depends on 2.3).

## Sequencing

Tier 1 first — defect 1 breaks the first real tool-using agent anyone builds. Then 3.1 ahead of most
of Tier 2: first-party SDKs will always match or beat us on loop features; none offer a self-hosted,
provider- and harness-neutral audit and ops layer.
