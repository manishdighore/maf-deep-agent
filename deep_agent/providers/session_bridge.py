"""SessionBridgeProvider — hydrates the shared skill state from session on turn start.

``ChatMiddleware`` has no access to the session object, so skill-enabled state
must be communicated via a shared mutable dict that both
``SkillToolkitMiddleware`` (FunctionMiddleware) and ``SkillToolFilterMiddleware``
(ChatMiddleware) hold a reference to.

This provider runs in ``before_run()`` — before any LLM call — and copies
``session.state["enabled_toolkits"]`` into the shared dict.  This handles the
session-reload case: on a fresh turn after restoring a persisted session, the
shared dict (which is an ephemeral in-memory object) gets populated from the
durable session state so that ``SkillToolFilterMiddleware`` sees the correct
enabled skills from the very first LLM call.

Mid-turn updates (``load_skill``) are handled by ``SkillToolkitMiddleware``,
which writes to both ``session.state`` and the shared dict in real-time.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework._sessions import AgentSession, ContextProvider, SessionContext

from deep_agent._logging import agent_log

logger = logging.getLogger(__name__)


class SessionBridgeProvider(ContextProvider):
    """Hydrates the shared skill state dict from session.state at turn start."""

    def __init__(self, shared_state: dict[str, Any]) -> None:
        super().__init__("session_bridge")
        self._shared = shared_state

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        enabled: set[str] = session.state.get("enabled_toolkits", set())
        self._shared["enabled_toolkits"] = set(enabled)  # copy to avoid aliasing

        if enabled:
            agent_log(
                "SessionBridgeProvider",
                "hydrated",
                f"enabled_toolkits from session: {sorted(enabled)}",
            )
