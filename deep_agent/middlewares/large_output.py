"""LargeOutputMiddleware — spills oversized tool results to the virtual filesystem.

When a tool's output exceeds ``threshold`` estimated tokens (chars ÷ 4), this
middleware:

1. Writes the **full** output (with a metadata header) into the session-scoped
   ``ThreadedStateFilesystem`` under ``/.outputs/{tool}_{call_id}.md``.
2. Replaces ``context.result`` with a truncated version that includes head/tail
   excerpts and the file path so the agent can ``read_file`` the rest.

This keeps the LLM context lean while preserving all data for on-demand access.

Usage::

    from deep_agent.services.filesystem import ThreadedStateFilesystem
    from deep_agent.middlewares import LargeOutputMiddleware

    fs = ThreadedStateFilesystem()
    middleware = LargeOutputMiddleware(fs=fs)  # spills at ~1,000 tokens
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Awaitable
from typing import Any

from agent_framework._middleware import FunctionMiddleware, FunctionInvocationContext

from deep_agent._logging import agent_log
from deep_agent.services.filesystem import ThreadedStateFilesystem

logger = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 4 characters (same heuristic as DeepAgents)
CHARS_PER_TOKEN = 4

# Tools whose output should never be spilled (filesystem tools themselves,
# to avoid circular writes).
_DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset({
    "ls", "read_file", "write_file", "edit_file", "glob", "grep",
})


def _sanitize_call_id(call_id: str) -> str:
    """Strip dangerous chars from a tool_call_id for safe use in paths."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", call_id)


def _extract_text(result: Any) -> str | None:
    """Best-effort extraction of a single string from a tool result.

    ``context.result`` can be:
      - ``str``
      - ``list[Content]`` where each Content has ``.text``
      - something else entirely (return None)
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text") and isinstance(item.text, str):
                parts.append(item.text)
        if parts:
            return "\n".join(parts)
    return None


def _format_args(arguments: Any) -> str:
    """Render tool arguments as compact JSON for the file header."""
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False, default=str)
    if hasattr(arguments, "model_dump"):
        return json.dumps(arguments.model_dump(), ensure_ascii=False, default=str)
    return str(arguments)


class LargeOutputMiddleware(FunctionMiddleware):
    """Intercepts large tool outputs and spills them to the filesystem.

    Parameters
    ----------
    fs : ThreadedStateFilesystem
        Shared filesystem instance (same one used by ``FilesystemProvider``).
    threshold : int
        Estimated token count to trigger spilling (default 5 000 tokens,
        i.e. ~20 000 chars using the chars÷4 heuristic).
    head : int
        Characters to keep at the start of the truncated result (default 500).
    tail : int
        Characters to keep at the end of the truncated result (default 200).
    exclude_tools : set[str] | None
        Tool names to never spill.  Defaults to the 6 filesystem tools.
        Pass an empty set to disable exclusions.
    """

    def __init__(
        self,
        fs: ThreadedStateFilesystem,
        *,
        threshold: int = 5_000,
        head: int = 500,
        tail: int = 200,
        exclude_tools: set[str] | frozenset[str] | None = None,
    ) -> None:
        self._fs = fs
        self._threshold = threshold
        self._head = head
        self._tail = tail
        self._exclude_tools: frozenset[str] = (
            frozenset(exclude_tools) if exclude_tools is not None
            else _DEFAULT_EXCLUDE_TOOLS
        )

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        # Let the tool execute normally
        await call_next()

        # Skip if no result, excluded tool, or no session
        tool_name = context.function.name
        if tool_name in self._exclude_tools:
            return
        if context.result is None or context.session is None:
            return

        # Extract text from result
        text = _extract_text(context.result)
        if text is None:
            return

        est_tokens = len(text) // CHARS_PER_TOKEN
        if est_tokens <= self._threshold:
            return

        # ── Spill to filesystem ─────────────────────────────────────

        tid = context.session.session_id

        # Build a safe filename from tool name + call_id
        call_id = context.metadata.get("call_id", "")
        if not call_id:
            # Fallback: use kwargs which may carry tool_call_id
            call_id = context.kwargs.get("tool_call_id", "unknown")
        safe_id = _sanitize_call_id(call_id)
        file_path = f"/.outputs/{tool_name}_{safe_id}.md"

        # Build file content with metadata header
        args_str = _format_args(context.arguments)
        header = (
            f"# Tool Output: {tool_name}\n"
            f"**Call ID:** {call_id}\n"
            f"**Args:** {args_str}\n"
            f"**Length:** {len(text):,} chars (~{est_tokens:,} tokens)\n"
            f"---\n"
        )
        full_content = header + text

        # Write to filesystem (overwrite in case of call_id collision)
        self._fs.write(tid, file_path, full_content, overwrite=True)

        # ── Replace context.result with truncated version ───────────

        head_text = text[:self._head]
        tail_text = text[-self._tail:] if self._tail > 0 else ""

        truncated_parts = [head_text]
        if tail_text:
            truncated_parts.append(f"\n\n[... {len(text) - self._head - self._tail:,} chars omitted ...]\n\n")
            truncated_parts.append(tail_text)

        truncated_parts.append(
            f"\n\n⚠️ Output truncated (~{est_tokens:,} tokens, {len(text):,} chars). "
            f"Full result saved to: {file_path}\n"
            f'Use read_file("{file_path}") to view.'
        )

        context.result = "".join(truncated_parts)

        agent_log(
            "LargeOutputMiddleware", "spilled",
            f"{tool_name} → ~{est_tokens:,} tokens ({len(text):,} chars) → {file_path}",
        )
