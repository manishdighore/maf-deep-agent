"""ToolkitInjectorProvider — injects enabled toolkit tools per turn."""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool
from agent_framework._sessions import AgentSession, ContextProvider, SessionContext


class ToolkitInjectorProvider(ContextProvider):
    """Reads session.state["enabled_toolkits"] and calls context.extend_tools()
    for each enabled toolkit every turn."""

    def __init__(self, skill_toolkits: dict[str, list[FunctionTool]]) -> None:
        super().__init__("toolkit_injector")
        self.skill_toolkits = skill_toolkits

    async def before_run(self, *, agent: Any, session: AgentSession,
                         context: SessionContext, state: dict[str, Any]) -> None:
        enabled: set[str] = session.state.get("enabled_toolkits", set())
        for skill_name in enabled:
            tools = self.skill_toolkits.get(skill_name, [])
            if tools:
                context.extend_tools(self.source_id, tools)
