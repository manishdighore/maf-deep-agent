"""Example: using deep_agent to build a generalist agent.

This mirrors the setup in generalist_agent/ but uses the
`create_deep_agent` factory function instead of manual wiring.

Usage:
    # Set env vars first:
    #   AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT
    #   (or OPENAI_API_KEY for OpenAI direct)
    #
    # Then run:
    #   cd maf-framework
    #   uv run python deep_agent/examples/generalist.py
"""

from __future__ import annotations

import asyncio
import os
from textwrap import dedent

from dotenv import load_dotenv
load_dotenv()

from agent_framework._skills import InlineSkill
from agent_framework_openai import OpenAIChatClient

from deep_agent import create_deep_agent


# ── 1. Client ──────────────────────────────────────────────

def make_client() -> OpenAIChatClient:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    if endpoint and api_key:
        return OpenAIChatClient(
            model=deployment,
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version="2025-04-01-preview",
        )
    # Fallback: direct OpenAI
    return OpenAIChatClient(model="gpt-4o")


# ── 2. Skills ──────────────────────────────────────────────

web_research_skill = InlineSkill(
    name="web-research",
    description="Web search via Tavily.",
    instructions=dedent("""\
        Use this skill to search the web for information.
        Tools available after loading:
        - tavily_search: search the web
    """),
)

code_execution_skill = InlineSkill(
    name="code-execution",
    description="Run code in a sandboxed environment.",
    instructions=dedent("""\
        Use this skill to run code in a sandbox.
        Always print() results explicitly.
    """),
)

SKILLS = [web_research_skill, code_execution_skill]

# Mutable dict — populate after async tool init
SKILL_TOOLKITS: dict[str, list] = {
    "web-research": [],
    "code-execution": [],
}


# ── 3. Build agent ─────────────────────────────────────────

def build_agent():
    client = make_client()

    return create_deep_agent(
        name="example-generalist",
        client=client,
        instructions=dedent(f"""\
            You are a helpful AI assistant with access to skills.

            ## Base Tools (always available)
            - load_skill: Load a skill to unlock its tools
            - todo: Manage a task list
            - delegate_task: Spawn a sub-agent for complex tasks

            ## Skills (on-demand via load_skill)
            Skills and their tools are auto-documented when loaded.

            ## Guidelines
            - Load skills before using their tools
            - Be helpful, concise, and proactive
            - Use delegate_task only for complex multi-step work
        """),
        skills=SKILLS,
        skill_toolkits=SKILL_TOOLKITS,
        # tools=[],             # add always-on tools here
        # target_count=8,       # compaction tuning
        # threshold=12,         # compaction tuning
        # enable_todo=True,     # toggle todo provider
        # enable_delegation=True,  # toggle delegation
    )


# ── 4. Run (CLI mode) ─────────────────────────────────────

async def main():
    agent = build_agent()

    # NOTE: In a real app, you'd populate SKILL_TOOLKITS here
    # after initializing MCP tools or other async resources.
    # e.g.: SKILL_TOOLKITS["web-research"] = [tavily_tool]

    session = agent.create_session()
    print("🤖 Deep Agent ready. Type 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        response = await agent.run(user_input, session=session)
        print(f"\nAgent: {response.text}\n")


if __name__ == "__main__":
    asyncio.run(main())
