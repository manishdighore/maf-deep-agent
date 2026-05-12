"""Rich-based terminal logging for agent components."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.theme import Theme

_theme = Theme({
    "component": "bold cyan",
    "action": "bold yellow",
    "detail": "white",
    "data": "dim",
    "success": "bold green",
    "spawn": "bold magenta",
})

console = Console(theme=_theme)

_ICONS: dict[str, str] = {
    "compacted": "🗜️",
    "toolkit_offloaded": "📦",
    "hint_injected": "💡",
    "skill_loaded": "🔧",
    "updated": "📝",
    "spawn": "🚀",
    "done": "✅",
    "pruned": "🧹",
    "persisted": "💾",
}


def agent_log(component: str, action: str, detail: str, *, data: dict[str, Any] | None = None) -> None:
    """Print a rich-formatted log line to the terminal."""
    icon = _ICONS.get(action, "•")
    parts = f"[component]{component}[/] [action]{action}[/] {icon}  {detail}"
    if data:
        parts += f"  [data]{data}[/]"
    console.print(parts)
