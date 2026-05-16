"""SkillToolFilterMiddleware — per-LLM-call tool list gating based on enabled skills."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import FunctionTool
from agent_framework._middleware import ChatContext, ChatMiddleware
from rich.panel import Panel
from rich.table import Table

from deep_agent._logging import agent_log, console

# TODO: remove before release — set to False to disable diagnostic panels
_DEBUG = False


class SkillToolFilterMiddleware(ChatMiddleware):
    """Gates which tools the LLM sees before each LLM call within an agent turn.

    Background
    ----------
    All skill tools are registered on the agent at build time so the
    framework's function-invocation registry can always execute them.
    Without filtering, every skill's tools would appear in every LLM call,
    even before the skill has been loaded — cluttering the schema and
    confusing the model.

    This middleware intercepts ``context.options["tools"]`` and rebuilds
    the list from three buckets:

    1. **Provider tools** — contributed at turn-start by ``ContextProvider.before_run()``
       (e.g. ``load_skill``, ``todo``, ``delegate_task``, filesystem tools, any custom
       provider tools).  These are *not* in ``self._all_tools`` (which only holds
       build-time tools) so they are detected by exclusion and always passed through
       untouched.

    2. **Base tools** — passed as ``tools=`` at agent build time (e.g. ``plot_data``,
       ``add_memory``).  These are in ``self._all_tools`` but have no owning skill in
       ``_tool_to_skill``, so they are always included.

    3. **Skill tools** — also in ``self._all_tools`` and mapped to a skill name in
       ``_tool_to_skill``.  Only included when their owning skill is present in
       ``shared_state["enabled_toolkits"]``, which is written by:
       - ``SessionBridgeProvider.before_run()`` — hydrated from session.state at turn start
       - ``SkillToolkitMiddleware`` — updated mid-turn after ``load_skill`` executes

    Same-turn availability
    ----------------------
    Because ``ChatMiddleware.process()`` fires once per LLM call (inside the
    ``FunctionInvocationLayer`` tool loop, not once per agent turn), skill tools
    become visible to the LLM on the **very next LLM call** after ``load_skill``
    runs — within the same agent turn.

    Mutation safety
    ---------------
    The filter always rebuilds from ``self._all_tools``, never from the incoming
    ``context.options["tools"]``.  This is critical: ``FunctionInvocationLayer``
    reuses the same ``mutable_options`` dict across all loop iterations, so
    reading from ``context.options["tools"]`` after the first iteration would
    operate on an already-filtered list, making it impossible to re-enable tools.
    """

    def __init__(
        self,
        all_tools: list[FunctionTool],
        skill_toolkits: dict[str, list[FunctionTool]],
        shared_state: dict[str, Any],
    ) -> None:
        """
        Args:
            all_tools: Complete flat list of every tool registered on the agent
                at build time (base tools + all skill toolkit tools). This is the
                authoritative source — never read from context.options["tools"].
            skill_toolkits: Mapping of skill_name → tool list, used to build
                the reverse map of tool_name → skill_name.
            shared_state: Shared mutable dict also held by SessionBridgeProvider
                and SkillToolkitMiddleware. Contains "enabled_toolkits" set.
        """
        # Reverse map: tool_name → skill_name (only for skill tools)
        self._tool_to_skill: dict[str, str] = {}
        for skill_name, tools in skill_toolkits.items():
            for t in tools:
                self._tool_to_skill[t.name] = skill_name

        self._all_tools = list(all_tools)
        self._shared = shared_state

    @staticmethod
    def _tool_name(t: Any) -> str:
        if hasattr(t, "name"):
            return t.name
        if isinstance(t, dict):
            return t.get("function", t).get("name", "")
        return ""

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        enabled: set[str] = self._shared.get("enabled_toolkits", set())

        if _DEBUG:
            self._print_debug(context, enabled)

        # Names of every tool we manage (base tools + all skill tools).
        # Anything outside this set is a provider-contributed tool (load_skill,
        # todo, delegate_task, filesystem tools, custom context provider tools)
        # that we must NEVER drop — pass them all through unconditionally.
        known_names: set[str] = {self._tool_name(t) for t in self._all_tools}

        current_tools: list[Any] = (
            list(context.options.get("tools") or []) if context.options else []
        )
        # Provider tools: present in context.options["tools"] but not in our managed set.
        # These come from ContextProvider.before_run() and are always visible.
        provider_tools: list[Any] = [
            t for t in current_tools if self._tool_name(t) not in known_names
        ]

        # Filter managed tools: base tools always visible, skill tools only when enabled.
        # Rebuild from self._all_tools — NOT from current_tools — to avoid
        # double-mutation across iterations in the same agent turn.
        managed_filtered: list[Any] = []
        for t in self._all_tools:
            name = self._tool_name(t)
            owning_skill = self._tool_to_skill.get(name)
            if owning_skill is None:
                managed_filtered.append(t)
            elif owning_skill in enabled:
                managed_filtered.append(t)

        final_tools = provider_tools + managed_filtered
        before_count = len(current_tools)
        context.options["tools"] = final_tools  # type: ignore[index]

        if enabled:
            agent_log(
                "SkillToolFilterMiddleware",
                "tools_filtered",
                f"{before_count} → {len(final_tools)} tools  enabled_skills={sorted(enabled)}",
                data={"enabled_skills": sorted(enabled), "tool_count": len(final_tools)},
            )

        await call_next()

    def _print_debug(
        self,
        context: ChatContext,
        enabled: set[str],
    ) -> None:
        """Rich diagnostic panel — only shown when _DEBUG is True."""
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold magenta", min_width=22)
        t.add_column()

        opts = context.options or {}
        option_keys = sorted(k for k in opts if k != "tools")
        current_tools = [self._tool_name(x) for x in (opts.get("tools") or [])]
        known = sorted(self._tool_to_skill.keys())

        t.add_row("[cyan]middleware[/]",         "SkillToolFilterMiddleware")
        t.add_row("[cyan]shared_state[/]",        str(dict(self._shared)))
        t.add_row("[cyan]enabled_toolkits[/]",    str(sorted(enabled)) if enabled else "∅")
        t.add_row("[cyan]option_keys[/]",         str(option_keys))
        t.add_row("[cyan]current_tools[/]",       str(current_tools))
        t.add_row("[cyan]known_skill_tools[/]",   str(known))
        t.add_row("[cyan]_all_tools count[/]",    str(len(self._all_tools)))
        t.add_row("[cyan]_tool_to_skill[/]",      str(self._tool_to_skill))

        console.print(Panel(t, title="[bold magenta]🔍 SkillToolFilter DEBUG[/]",
                            border_style="magenta", expand=False))
