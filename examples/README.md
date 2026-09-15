# Examples

Every example is a single runnable file. They go in roughly increasing order of surface area — start at the top.

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
python examples/hello_agent.py
```

| Example | Lines | What it shows | Needs |
|---|---|---|---|
| [`hello_agent.py`](hello_agent.py) | 16 | The minimal agent — one provider, one `run()`, cost and token accounting for free. | API key |
| [`pipeline_example.py`](pipeline_example.py) | 40 | `LinearPipeline` — a three-stage research → draft → edit chain where each agent's output feeds the next. | API key |
| [`multi_tool_agent.py`](multi_tool_agent.py) | 54 | The `@tool` decorator with several tools and console tracing. Calls live public APIs (CoinGecko), so no second key is needed. | API key |
| [`research_dag.py`](research_dag.py) | 85 | `DAGOrchestrator` — three researchers run concurrently, a fourth synthesizes. Prints the wall-clock speedup over sequential execution. | API key |
| [`safe_agent.py`](safe_agent.py) | 111 | The production posture: tool allowlisting, the tamper-evident Merkle audit chain, chain verification, and JSONL export for compliance. | API key |
| [`claude_agent_sdk_monitored.py`](claude_agent_sdk_monitored.py) | 31 | A Claude Agent SDK run reported to agent-kit Cloud through hooks and the message stream. | `pip install agent-kit-ai[claude-agent-sdk]`; Claude Code auth; `AGENTKIT_API_KEY` |
| [`openai_agents_monitored.py`](openai_agents_monitored.py) | 33 | An OpenAI Agents SDK run reported to agent-kit Cloud through a trace processor. | `pip install agent-kit-ai[openai-agents]`; `OPENAI_API_KEY`; `AGENTKIT_API_KEY` |
| [`typed_output.py`](typed_output.py) | 47 | Typed results — an agent looks up prices with a tool and returns a validated `Quote` model. | API key |
| [`long_running_agent.py`](long_running_agent.py) | 42 | Context management — prompt caching, effort, server-side compaction and tool-result clearing on a 25-section handbook audit. | API key |
| [`scanned_tools.py`](scanned_tools.py) | 53 | Tool output scanning — a pricing agent reads three pages; the one with hidden instructions is blocked, the one with a tracking pixel is marked untrusted. Set `NULLCONE=1` to add threat-intel lookups. | API key |
| [`delegation.py`](delegation.py) | 72 | Agents as tools — a support lead delegates to a researcher and a refunds agent; the refund's approval suspends the whole ticket (`start`) and `approve <call_id>` resumes it. | API key |
| [`durable_approval.py`](durable_approval.py) | 47 | Durable runs — a refund suspends for human approval (`start`), and a separate invocation approves and completes it (`approve <call_id>`). | API key |
| [`mcp_tools.py`](mcp_tools.py) | 39 | The official filesystem MCP server's tools inside an agent, with approval required for anything not marked read-only. | API key; Node.js; `pip install agent-kit-ai[mcp]` |
| [`approval_gate.py`](approval_gate.py) | 72 | Hooks and approval gates — a terminal approver for refunds, card numbers redacted from tool output, and account closure stopping the run. | API key |
| [`cloud_monitored.py`](cloud_monitored.py) | 148 | Full fleet observability — `CloudReporter` shipping lifecycle events to agent-kit Cloud, plus circuit breaker config and per-run cost attribution. Runs locally with cloud reporting off when `AGENTKIT_API_KEY` is unset. | API key; cloud optional |

## Notes

- **`ANTHROPIC_API_KEY`** is read from the environment by `AnthropicProvider()` — no example takes a key as an argument. To run against OpenAI or a local Ollama model instead, swap the provider; see the Providers section of the [README](../README.md#providers).
- **These make real API calls and cost real money.** `hello_agent.py` is a fraction of a cent; `research_dag.py` runs four agents.
- **`cloud_monitored.py` runs without a backend.** Set `AGENTKIT_API_KEY` to ship events to agent-kit Cloud (`https://ingest.agentkit.io` by default; pass `base_url=` to `CloudReporter` for a self-hosted server) — see [`docs/cloud-quickstart.md`](../docs/cloud-quickstart.md).
- Three of these are reproduced with their real console output in the [README demos](../README.md#demos).

CI byte-compiles every file in this directory, so an example that stops importing fails the build.
