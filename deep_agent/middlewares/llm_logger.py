"""LLMCallLogMiddleware — rich panel showing what goes to the LLM."""

from __future__ import annotations

import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework._middleware import ChatContext, ChatMiddleware
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deep_agent._logging import console


class LLMCallLogMiddleware(ChatMiddleware):
    """Logs a rich panel before each LLM call showing messages, tools, and system prompt."""

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        messages = context.messages
        options = context.options or {}

        role_counts: Counter[str] = Counter()
        system_prompt_preview = ""
        total_chars = 0
        for msg in messages:
            role_counts[msg.role] += 1
            text = ""
            if hasattr(msg, "contents") and msg.contents:
                for c in msg.contents:
                    if isinstance(c, str):
                        text += c
                    elif hasattr(c, "text"):
                        text += getattr(c, "text", "") or ""
            total_chars += len(text)
            if msg.role == "system" and not system_prompt_preview:
                system_prompt_preview = text[:200].replace("\n", " ").strip()
                if len(text) > 200:
                    system_prompt_preview += "…"

        tools = options.get("tools") or []
        tool_names = []
        for t in tools:
            if hasattr(t, "name"):
                tool_names.append(t.name)
            elif isinstance(t, dict):
                fn = t.get("function", t)
                tool_names.append(fn.get("name", "?"))

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", min_width=14)
        table.add_column()

        role_str = "  ".join(f"{role}={count}" for role, count in sorted(role_counts.items()))
        est_tokens = max(1, total_chars // 4)
        table.add_row("Messages", f"{len(messages)} total  ({role_str})  ~{est_tokens:,} est_tokens  ({total_chars:,} chars)")
        table.add_row("Tools", f"{len(tool_names)}: {', '.join(tool_names) if tool_names else '(none)'}")
        if system_prompt_preview:
            table.add_row("System", f"[dim]{system_prompt_preview}[/dim]")

        panel = Panel(table, title="[bold yellow]→ LLM Call[/]", border_style="yellow", expand=False)
        console.print(panel)

        try:
            await call_next()
        except Exception:
            tb = traceback.format_exc()
            err_panel = Panel(
                Text(tb, style="red"),
                title="[bold red]💥 Agent Error (full traceback)[/]",
                border_style="red",
                expand=False,
            )
            console.print(err_panel)
            raise
