"""Three-stage research → draft → edit pipeline."""

import asyncio
from agent_kit import Agent, AgentConfig
from agent_kit.orchestrator import LinearPipeline
from agent_kit.providers import AnthropicProvider


async def main():
    provider = AnthropicProvider()

    researcher = Agent(
        provider,
        config=AgentConfig(system_prompt="You are a researcher. Summarize key facts on the topic."),
    )
    writer = Agent(
        provider,
        config=AgentConfig(system_prompt="You are a technical writer. Write a clear 3-paragraph blog post."),
    )
    editor = Agent(
        provider,
        config=AgentConfig(system_prompt="You are an editor. Tighten the prose, fix grammar, keep under 300 words."),
    )

    pipeline = LinearPipeline([
        (researcher, "Research this topic and provide 5 key facts: {input}"),
        (writer,     "Write a blog post based on these facts: {input}"),
        (editor,     "Edit and polish this post: {input}"),
    ])

    result = await pipeline.run("The impact of circuit breakers on production AI agent systems")

    print("=== Final Post ===")
    print(result.final_output)
    print(f"\nTotal cost: ${result.total_cost_usd:.4f} across {len(result.stage_results)} stages")
    print(f"Total tokens: {result.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
