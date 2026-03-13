# Troubleshooting

Common issues, their root causes, and how to resolve them.

---

## Circuit breaker stuck in OPEN state

**Symptom:** An agent is raising `CircuitOpenError` and not recovering, even though the underlying provider appears healthy.

**Root cause:** The circuit breaker opened after `failure_threshold` consecutive failures and has not yet attempted recovery. By default, recovery requires waiting `recovery_timeout_s` (60s) before the breaker enters HALF_OPEN and probes the provider.

**Resolution steps:**

1. **Wait for auto-recovery.** The breaker transitions to HALF_OPEN after `recovery_timeout_s`. If the next call succeeds, it returns to CLOSED.

2. **Check the provider is actually healthy.** A breaker stuck open often means the underlying error is still occurring:
   ```python
   # Temporarily bypass the breaker to test the provider directly
   result = await agent.provider.complete(messages=[...], model=...)
   ```

3. **Reduce `recovery_timeout_s` for faster recovery:**
   ```python
   from agent_kit.types import CircuitBreakerConfig
   config = AgentConfig(
       circuit_breaker=CircuitBreakerConfig(
           recovery_timeout_s=15.0,   # reduce from 60s default
           success_threshold=1,        # close after 1 successful probe
       )
   )
   ```

4. **In the cloud dashboard:** Navigate to Metrics → Circuit Breaker to see the state history and when the breaker opened. The `duration_open_ms` field shows how long it has been open.

5. **If using cloud alerting:** The `circuit_breaker_open` alert will fire automatically. Check `/v1/alerts/firing` to see the firing record, and acknowledge it once resolved with `POST /v1/alerts/firing/{id}/ack`.

---

## Audit integrity failure

**Symptom:** An audit run shows `integrity: "failed"` in the dashboard or `GET /v1/audit/runs/{run_id}/verify` returns `verified: false`.

**Root cause:** The Merkle chain stored server-side does not match what the SDK sent. This can happen due to:
- Data corruption in transit (rare with HTTPS)
- A bug that modified events after they were hashed but before they were shipped
- Tampering (the audit trail is designed to detect this)

**Resolution steps:**

1. **Identify the broken link:**
   ```bash
   curl -H "Authorization: Bearer $AGENTKIT_API_KEY" \
     https://ingest.agentkit.io/v1/audit/runs/{run_id}/verify
   ```
   The response includes `broken_at_seq` — the sequence number of the first invalid event.

2. **Compare with the local audit chain:**
   ```python
   # If audit_enabled=True, the SDK holds the chain in memory during the run
   result = await agent.run(...)
   chain_valid = agent.audit.verify()
   print(f"Local chain valid: {chain_valid}")
   print(f"Events: {agent.audit.export_jsonl()}")
   ```

3. **Re-import from JSONL export.** If you exported the chain after the run, you can verify it offline:
   ```python
   from agent_kit.audit import AuditChain
   chain = AuditChain.from_jsonl(open("audit.jsonl").read())
   print(chain.verify())
   ```

4. **Alert behaviour.** `audit_integrity_failure` alerts **never auto-resolve** — they require manual investigation and acknowledgement. This is by design: a tampered audit trail should not silently clear.

---

## Unexpected cost spike

**Symptom:** `total_cost_usd` in the metrics dashboard is much higher than expected. A `cost_anomaly` alert has fired.

**Resolution steps:**

1. **Identify the source.** Use the cost endpoint to drill down:
   ```bash
   curl "https://ingest.agentkit.io/v1/metrics/cost?group_by=agent_name&resolution=1h" \
     -H "Authorization: Bearer $AGENTKIT_API_KEY"
   ```
   Compare series to find which agent's cost increased.

2. **Check run count and turns.** A cost spike could be more runs, longer turns, or a model change:
   ```bash
   curl "https://ingest.agentkit.io/v1/metrics/runs?agent_name=billing-agent" \
     -H "Authorization: Bearer $AGENTKIT_API_KEY"
   ```
   Look at `avg_turns` — a sudden increase suggests the agent is looping.

3. **Look for tool call loops.** If `avg_turns` is high, the agent may be calling tools repeatedly without reaching a conclusion. Add an explicit turn limit:
   ```python
   config = AgentConfig(max_turns=10)
   ```

4. **Check for expensive model usage.** If a cheaper model was accidentally replaced with a more expensive one:
   ```bash
   curl "https://ingest.agentkit.io/v1/metrics/cost?group_by=model" \
     -H "Authorization: Bearer $AGENTKIT_API_KEY"
   ```

5. **Set a cost anomaly alert** to catch future spikes:
   ```bash
   curl -X POST https://ingest.agentkit.io/v1/alerts/rules \
     -H "Authorization: Bearer $AGENTKIT_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Daily cost > $50",
       "type": "cost_anomaly",
       "config": {"threshold_usd": 50.0, "window_hours": 24},
       "channel_ids": ["your-channel-id"]
     }'
   ```

---

## Migrating from LangChain or CrewAI

### LangChain

LangChain agents use `AgentExecutor`. Replace it with an `Agent` + tools pattern:

**Before (LangChain)**

```python
from langchain.agents import AgentExecutor, create_react_agent
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-sonnet-4-6")
agent = create_react_agent(llm, tools, prompt)
executor = AgentExecutor(agent=agent, tools=tools)
result = executor.invoke({"input": "What is the Bitcoin price?"})
```

**After (agent-kit)**

```python
from agent_kit import Agent, AgentConfig, tool
from agent_kit.providers import AnthropicProvider

@tool(description="Get the current Bitcoin price")
async def get_btc_price() -> dict:
    ...

agent = Agent(
    provider=AnthropicProvider(),
    tools=[get_btc_price],
    config=AgentConfig(audit_enabled=True),
)
result = await agent.run("What is the Bitcoin price?")
```

Key differences:
- Tools are plain `async` functions decorated with `@tool`, not `BaseTool` subclasses
- No manual prompt template needed — the system prompt goes in `AgentConfig`
- The retry policy, circuit breaker, and audit trail are built in

### CrewAI

CrewAI's `Crew` maps to agent-kit's `DAGOrchestrator` or `LinearPipeline`.

**Before (CrewAI)**

```python
from crewai import Agent, Task, Crew

researcher = Agent(role="Researcher", ...)
writer = Agent(role="Writer", ...)
crew = Crew(agents=[researcher, writer], tasks=[task1, task2], process="sequential")
result = crew.kickoff()
```

**After (agent-kit)**

```python
from agent_kit.orchestrator import LinearPipeline

pipeline = LinearPipeline([
    (researcher_agent, "Research this topic: {input}"),
    (writer_agent,     "Write a post from these facts: {input}"),
])
result = await pipeline.run("The future of AI agents")
print(result.final_output)
print(f"Total cost: ${result.total_cost_usd:.4f}")
```

For parallel stages with dependencies, use `DAGOrchestrator`:

```python
from agent_kit.orchestrator import DAGOrchestrator

dag = DAGOrchestrator()
dag.add_node("research", researcher_agent, "Research: {input}")
dag.add_node("draft",    writer_agent,     "Write: {input}", depends_on=["research"])
dag.add_node("edit",     editor_agent,     "Edit: {input}",  depends_on=["draft"])
result = await dag.run("The future of AI agents")
```

---

## CloudReporter not sending events

**Symptom:** The fleet dashboard shows no data, but the agent is running.

**Checklist:**

1. **Verify the API key is set:**
   ```python
   import os
   print(os.environ.get("AGENTKIT_API_KEY"))  # should print your key
   ```

2. **Verify `cloud=reporter` is in AgentConfig:**
   ```python
   config = AgentConfig(cloud=reporter)  # easy to forget
   ```

3. **Check for queue overflow.** If events are being dropped due to a full queue, you'll see `DEBUG` log messages:
   ```bash
   export PYTHONPATH=. AGENTKIT_LOG_LEVEL=DEBUG python your_agent.py
   ```
   Increase `max_queue_size` or decrease `flush_interval_s` if the queue is filling up.

4. **Manually flush in tests or short-lived scripts:**
   ```python
   await reporter.flush()
   # or at shutdown:
   await reporter.close()
   ```
   `CloudReporter` batches events every 5 seconds. A script that finishes in 1 second may not flush automatically. The `atexit` handler attempts a synchronous flush, but `asyncio` event loops may already be closed at that point.

5. **Check network connectivity:**
   ```bash
   curl -I https://ingest.agentkit.io/healthz
   ```

6. **Self-hosted server:** Confirm `base_url` matches your server address, including any path prefix.

---

## Error rate alert firing continuously

**Symptom:** An `error_rate` alert fires every evaluation cycle (every 60 seconds) even after fixing the underlying issue.

**Root cause:** The alert evaluator looks at a rolling window (e.g. the last 24 hours). If many errors occurred earlier in the window, the rate stays elevated until the window slides past those errors.

**Resolution:**

- **Acknowledge the alert** to suppress notifications while you wait for the window to clear:
  ```bash
  curl -X POST https://ingest.agentkit.io/v1/alerts/firing/{id}/ack \
    -H "Authorization: Bearer $AGENTKIT_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"comment": "Fixed — waiting for rolling window to clear"}'
  ```

- **Mute the rule** temporarily:
  ```bash
  curl -X PATCH https://ingest.agentkit.io/v1/alerts/rules/{rule_id} \
    -H "Authorization: Bearer $AGENTKIT_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"muted_until": "2026-03-13T09:00:00"}'
  ```

- **Increase `min_runs`** to avoid alerts on small samples:
  ```json
  {"config": {"threshold_pct": 10.0, "window_hours": 24, "min_runs": 50}}
  ```
