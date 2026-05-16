"""SkillToolkitMiddleware — auto-enables toolkit on load_skill."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from typing import Any

from agent_framework import FunctionTool
from agent_framework._middleware import FunctionMiddleware, FunctionInvocationContext

from deep_agent._logging import agent_log

logger = logging.getLogger(__name__)


class SkillToolkitMiddleware(FunctionMiddleware):
    """Intercepts load_skill calls to write the skill name into
    session.state["enabled_toolkits"] AND the shared_state dict.

    Session state is the durable source of truth (survives reload).
    The shared dict is the live bridge that SkillToolFilterMiddleware
    (ChatMiddleware, no session access) reads on every LLM call.
    """

    def __init__(
        self,
        skill_toolkits: dict[str, list[FunctionTool]],
        shared_state: dict[str, Any],
    ) -> None:
        self.skill_toolkits = skill_toolkits
        self._shared = shared_state

    async def process(self, context: FunctionInvocationContext,
                      call_next: Callable[[], Any]) -> None:
        await call_next()

        if context.function.name == "load_skill":
            skill_name = ""
            if isinstance(context.arguments, dict):
                skill_name = context.arguments.get("skill_name", "")
            elif hasattr(context.arguments, "skill_name"):
                skill_name = getattr(context.arguments, "skill_name", "")

            if skill_name and skill_name in self.skill_toolkits and context.session:
                # Write to session state (durable, survives reload)
                enabled: set[str] = context.session.state.setdefault("enabled_toolkits", set())
                was_new = skill_name not in enabled
                enabled.add(skill_name)

                # Write to shared dict (live bridge for ChatMiddleware)
                shared_enabled: set[str] = self._shared.setdefault("enabled_toolkits", set())
                shared_enabled.add(skill_name)

                if was_new:
                    tool_names = [t.name for t in self.skill_toolkits[skill_name]]
                    agent_log("SkillToolkitMiddleware", "skill_loaded",
                              f"'{skill_name}' → +{len(tool_names)} tools",
                              data={"skill": skill_name, "tools": tool_names})
