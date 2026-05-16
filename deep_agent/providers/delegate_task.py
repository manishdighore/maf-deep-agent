"""DelegateTaskProvider — sub-agent spawning with concurrent batch."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_framework import Agent, AgentResponseUpdate, FunctionTool
from agent_framework._sessions import AgentSession, ContextProvider, SessionContext

from deep_agent._logging import agent_log
from deep_agent._streaming import active_subagent_queue

MAX_CONCURRENT_CHILDREN = 3
BLOCKED_CHILD_TOOLS = {"delegate_task", "todo"}

CHILD_SYSTEM_PROMPT = """\
You are a focused sub-agent working on a specific delegated task.

YOUR TASK:
{goal}

CONTEXT:
{context}

Complete this task using the tools available to you.
When finished, provide a clear, concise summary of what you did,
what you found, and any issues encountered."""


class DelegateTaskProvider(ContextProvider):
    def __init__(self, client: Any, skill_toolkits: dict[str, list[FunctionTool]], *,
                 base_tools: list[FunctionTool] | None = None,
                 source_id: str = "delegation", max_concurrent: int = MAX_CONCURRENT_CHILDREN) -> None:
        super().__init__(source_id)
        self.client = client
        self.skill_toolkits = skill_toolkits
        self.base_tools = base_tools or []
        self.max_concurrent = max_concurrent

    async def before_run(self, *, agent: Any, session: AgentSession,
                         context: SessionContext, state: dict[str, Any]) -> None:
        context.extend_tools(self.source_id, [self._make_delegate_tool()])

    def _build_child(self, name: str, goal: str, ctx: str, toolkits: list[str] | None) -> Agent:
        child_tools: list[FunctionTool] = []
        # Always include base tools (the parent's always-on tools)
        for tool in self.base_tools:
            if tool.name not in BLOCKED_CHILD_TOOLS:
                child_tools.append(tool)
        # Add requested skill toolkit tools
        for tk_name in (toolkits or []):
            for tool in self.skill_toolkits.get(tk_name, []):
                if tool.name not in BLOCKED_CHILD_TOOLS:
                    child_tools.append(tool)
        return Agent(
            client=self.client,
            name=name,
            instructions=CHILD_SYSTEM_PROMPT.format(goal=goal, context=ctx or "None provided."),
            tools=child_tools,
        )

    async def _run_child(self, name: str, goal: str, ctx: str, toolkits: list[str] | None) -> str:
        child_tool_names = [
            t.name for tk in (toolkits or [])
            for t in self.skill_toolkits.get(tk, [])
            if t.name not in BLOCKED_CHILD_TOOLS
        ]
        agent_log("DelegateTaskProvider", "spawn", f"[{name}] → {goal[:100]}",
                  data={"name": name, "goal": goal, "toolkits": toolkits or [], "child_tools": child_tool_names})
        child = self._build_child(name, goal, ctx, toolkits)

        queue = active_subagent_queue.get()
        try:
            if queue is not None:
                # Streaming mode — push tokens to caller's queue in real-time
                stream = child.run_stream(goal)
                async for update in stream:
                    update.author_name = name
                    await queue.put(update)
                response = await stream.get_final_response()
                await queue.put(AgentResponseUpdate(author_name=name, finish_reason="stop"))
            else:
                # No UI connected — run normally
                response = await child.run(goal)
        except asyncio.CancelledError:
            agent_log("DelegateTaskProvider", "cancelled", f"✗ [{name}] cancelled")
            if queue is not None:
                await queue.put(AgentResponseUpdate(author_name=name, finish_reason="stop"))
            raise

        text = response.text or "(sub-agent produced no text output)"
        agent_log("DelegateTaskProvider", "done", f"✓ [{name}] → {len(text)} chars")
        return text

    def _make_delegate_tool(self) -> FunctionTool:
        provider = self
        available_names = sorted(provider.skill_toolkits.keys())
        toolkit_list = ", ".join(f"'{n}'" for n in available_names)

        async def delegate_task(
            goal: str | None = None,
            name: str | None = None,
            context: str = "",
            toolkits: list[str] | None = None,
            tasks: list[dict[str, Any]] | None = None,
        ) -> str:
            if tasks and len(tasks) > 0:
                capped = tasks[:provider.max_concurrent]
                coros = [
                    provider._run_child(
                        t.get("name") or f"sub-agent-{i+1}",
                        t["goal"],
                        t.get("context", ""),
                        t.get("toolkits"),
                    )
                    for i, t in enumerate(capped)
                ]
                results = await asyncio.gather(*coros, return_exceptions=True)
                parts = []
                for i, (tk, result) in enumerate(zip(capped, results)):
                    label = tk.get("name") or tk["goal"][:60]
                    if isinstance(result, Exception):
                        parts.append(f"## {label}\nError: {result}")
                    else:
                        parts.append(f"## {label}\n{result}")
                return "\n\n".join(parts)
            elif goal:
                agent_name = name or "sub-agent"
                return await provider._run_child(agent_name, goal, context, toolkits)
            else:
                return "Error: provide either 'goal' or 'tasks'."

        return FunctionTool(
            name="delegate_task",
            description=(
                "Delegate a task to a focused sub-agent with fresh context. "
                "Sub-agents have NO memory — pass ALL info via context.\n\n"
                f"Available toolkits: {toolkit_list}.\n"
                "Single mode: name + goal + context + toolkits.\n"
                f"Batch mode: tasks array (up to {MAX_CONCURRENT_CHILDREN} concurrent)."
            ),
            func=delegate_task,
            input_model={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short descriptive name for this sub-agent (e.g. 'python-researcher', 'news-analyst'). Used in logs and streaming UI."},
                    "goal": {"type": "string", "description": "Task goal (single mode)."},
                    "context": {"type": "string", "description": "All relevant context."},
                    "toolkits": {"type": "array", "items": {"type": "string"},
                                 "description": f"Toolkits to equip. Available: {toolkit_list}."},
                    "tasks": {
                        "type": "array",
                        "description": "Batch mode: list of tasks to run concurrently.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Short descriptive name for this sub-agent."},
                                "goal": {"type": "string"},
                                "context": {"type": "string"},
                                "toolkits": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["goal"],
                        },
                    },
                },
                "required": [],
            },
        )
