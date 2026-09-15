"""
Claude Agent SDK run reported to agent-kit Cloud.

Requires: pip install agent-kit-ai[claude-agent-sdk], Claude Code authentication,
and AGENTKIT_BASE_URL + AGENTKIT_API_KEY for your agent-kit Cloud server.
"""

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from agent_kit.cloud import CloudReporter
from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver


async def main() -> None:
    reporter = CloudReporter(project="demo", agent_name="repo-explorer")
    observer = ClaudeAgentObserver(reporter)
    options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Glob", "Grep"], max_turns=6))

    prompt = "List the three largest Python modules in this repository and what each does."
    async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
        if isinstance(message, ResultMessage):
            print(message.result)
            print(f"turns={message.num_turns} cost=${message.total_cost_usd or 0:.4f}")

    await reporter.close()


if __name__ == "__main__":
    asyncio.run(main())
