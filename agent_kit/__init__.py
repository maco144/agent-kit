"""
agent-kit — Production-ready framework for building AI agents.

Type-safe. Observable. Circuit-broken.

Quick start::

    from agent_kit import Agent
    from agent_kit.providers import AnthropicProvider

    agent = Agent(AnthropicProvider())
    result = await agent.run("Explain the Monty Hall problem.")
    print(result.output)
    print(f"Cost: ${result.total_cost_usd:.4f}")
"""

from agent_kit.agent.agent import Agent, AgentConfig
from agent_kit.tools.base import Tool, tool
from agent_kit.types import AgentResult, Message, ToolResult, Turn

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # Core primitives
    "Agent",
    "AgentConfig",
    "Tool",
    "tool",
    # Result types
    "AgentResult",
    "Message",
    "Turn",
    "ToolResult",
]
