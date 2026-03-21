"""Production-safe agent — allowlisted tools, audit trail, cost guardrails.

Demonstrates:
  - Tool allowlisting (agent can only call approved tools)
  - Tamper-evident Merkle audit chain with verification
  - JSONL audit export for compliance
  - Per-run cost + token tracking

This is what makes agent-kit different: every agent action is recorded in a
hash-linked chain that proves no events were added, removed, or modified
after the fact. Try doing that with LangChain.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/safe_agent.py
"""

import asyncio
import json

from agent_kit import Agent, AgentConfig, tool
from agent_kit.providers import AnthropicProvider


# --- Define tools ---

@tool(description="Look up an employee's PTO balance by employee ID", idempotent=True)
async def check_pto(employee_id: str) -> dict:
    """Simulated HR system lookup."""
    balances = {
        "E001": {"name": "Alice Chen", "pto_days": 12, "used": 5},
        "E002": {"name": "Bob Park", "pto_days": 15, "used": 15},
        "E003": {"name": "Carol Reyes", "pto_days": 20, "used": 3},
    }
    if employee_id in balances:
        return balances[employee_id]
    return {"error": f"Employee {employee_id} not found"}


@tool(description="Submit a PTO request for an employee", idempotent=False)
async def submit_pto(employee_id: str, days: int, reason: str) -> dict:
    """Simulated PTO submission — would hit a real HR API in production."""
    return {
        "status": "approved",
        "employee_id": employee_id,
        "days": days,
        "reason": reason,
        "confirmation": "PTO-2026-0042",
    }


@tool(description="Delete an employee record permanently")
async def delete_employee(employee_id: str) -> dict:
    """Dangerous operation — should be blocked by allowlist."""
    return {"deleted": employee_id}


async def main():
    agent = Agent(
        provider=AnthropicProvider(),
        tools=[check_pto, submit_pto, delete_employee],
        config=AgentConfig(
            system_prompt=(
                "You are an HR assistant. Help employees check and request PTO. "
                "Always check the balance before submitting a request."
            ),
            # Only allow safe operations — delete_employee is registered but blocked
            allowed_tools=["check_pto", "submit_pto"],
            max_turns=10,
        ),
    )

    print("Running agent with tool allowlist: [check_pto, submit_pto]")
    print("(delete_employee is registered but BLOCKED)\n")

    result = await agent.run(
        "I'm employee E003. Can I take 5 days off next week for a family vacation?"
    )

    print("=" * 60)
    print("RESPONSE")
    print("=" * 60)
    print(result.output)

    # --- Audit trail ---
    print("\n" + "=" * 60)
    print("AUDIT TRAIL")
    print("=" * 60)

    chain = agent.audit
    print(f"Events recorded: {len(chain)}")
    print(f"Root hash: {chain.root_hash()}")
    print(f"Chain integrity: {'VERIFIED' if chain.verify() else 'BROKEN'}")

    print(f"\nCost: ${result.total_cost_usd:.4f} | Tokens: {result.total_tokens:,}")

    # Show the audit events
    print("\nEvent log:")
    for i, event in enumerate(chain.events()):
        print(f"  [{i}] {event.event_type:30s}  actor={event.actor[:20]}")

    # Export for compliance
    export = chain.export_jsonl()
    lines = export.strip().split("\n")
    print(f"\nJSONL export: {len(lines)} records, {len(export)} bytes")
    print(f"First record: {json.loads(lines[0])['event_type']}")
    print(f"Last record:  {json.loads(lines[-1])['event_type']}")


if __name__ == "__main__":
    asyncio.run(main())
