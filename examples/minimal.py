"""Minimal example — simplest possible deep_agent usage.

Just a client + instructions. No skills, no custom tools.
Todo and delegation are included automatically.

Usage:
    cd maf-framework
    uv run python deep_agent/examples/minimal.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
load_dotenv()  # loads .env from cwd or parents

from agent_framework_openai import OpenAIChatClient
from deep_agent import create_deep_agent


async def main():
    client = OpenAIChatClient(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
        api_version="2025-04-01-preview",
    )

    agent = create_deep_agent(
        client=client,
        instructions="You are a helpful assistant. Use the todo tool to track tasks.",
        enable_delegation=False,  # no delegation for this simple example
    )

    session = agent.create_session()
    print("🤖 Minimal deep agent. Type 'quit' to exit.\n")

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
