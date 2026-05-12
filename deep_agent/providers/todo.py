"""TodoProvider — todo tool + active item injection."""

from __future__ import annotations

import asyncio
import json
import weakref
from typing import Any

from agent_framework import FunctionTool, Message
from agent_framework._sessions import AgentSession, ContextProvider, SessionContext

from deep_agent._logging import agent_log

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


def _validate_todo(item: dict[str, Any]) -> dict[str, str]:
    item_id = str(item.get("id", "")).strip() or "?"
    content = str(item.get("content", "")).strip() or "(no description)"
    status = str(item.get("status", "pending")).strip().lower()
    if status not in VALID_STATUSES:
        status = "pending"
    return {"id": item_id, "content": content, "status": status}


class TodoProvider(ContextProvider):
    """Provides the todo tool and injects active items into context."""

    def __init__(self, *, source_id: str = "todo") -> None:
        super().__init__(source_id)
        self._locks: weakref.WeakKeyDictionary[AgentSession, asyncio.Lock] = weakref.WeakKeyDictionary()

    def _lock(self, session: AgentSession) -> asyncio.Lock:
        lock = self._locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session] = lock
        return lock

    async def before_run(self, *, agent: Any, session: AgentSession,
                         context: SessionContext, state: dict[str, Any]) -> None:
        context.extend_tools(self.source_id, [self._make_todo_tool(session)])

        todos: list[dict[str, str]] = session.state.get("todos", [])
        active = [t for t in todos if t.get("status") in ("pending", "in_progress")]
        if active:
            lines = ["[Active task list]"]
            for item in active:
                marker = ">" if item["status"] == "in_progress" else " "
                lines.append(f"- [{marker}] {item['id']}. {item['content']} ({item['status']})")
            context.extend_messages(self.source_id, [Message("system", ["\n".join(lines)])])

    def _make_todo_tool(self, session: AgentSession) -> FunctionTool:
        provider = self

        async def todo(
            todos: list[dict[str, Any]] | None = None,
            merge: bool = False,
        ) -> str:
            async with provider._lock(session):
                current: list[dict[str, str]] = session.state.get("todos", [])

            if todos is not None:
                if merge:
                    by_id = {t["id"]: dict(t) for t in current}
                    for item in todos:
                        item_id = str(item.get("id", "")).strip()
                        if not item_id:
                            continue
                        if item_id in by_id:
                            if "content" in item and item["content"]:
                                by_id[item_id]["content"] = str(item["content"]).strip()
                            if "status" in item and item["status"] in VALID_STATUSES:
                                by_id[item_id]["status"] = item["status"]
                        else:
                            by_id[item_id] = _validate_todo(item)
                    seen: set[str] = set()
                    rebuilt: list[dict[str, str]] = []
                    for t in current:
                        if t["id"] not in seen:
                            rebuilt.append(by_id.get(t["id"], t))
                            seen.add(t["id"])
                    for item_id, item in by_id.items():
                        if item_id not in seen:
                            rebuilt.append(item)
                    current = rebuilt
                else:
                    last_idx: dict[str, int] = {}
                    for i, item in enumerate(todos):
                        last_idx[str(item.get("id", "")).strip() or "?"] = i
                    current = [_validate_todo(todos[i]) for i in sorted(last_idx.values())]

                session.state["todos"] = current

            counts: dict[str, int] = {}
            for t in current:
                counts[t["status"]] = counts.get(t["status"], 0) + 1

            if todos is not None:
                mode = "merge" if merge else "replace"
                agent_log("TodoProvider", "updated",
                          f"{mode} → {len(current)} items", data={"summary": counts})

            return json.dumps({"todos": current, "summary": counts, "total": len(current)},
                              ensure_ascii=False)

        return FunctionTool(
            name="todo",
            description=(
                "Manage your task list for the current session. "
                "Call with no parameters to read the current list.\n\n"
                "Writing:\n"
                "- Provide 'todos' array to create/update items\n"
                "- merge=false (default): replace entire list\n"
                "- merge=true: update existing items by id, add new ones\n\n"
                "Each item: {id, content, status: pending|in_progress|completed|cancelled}\n"
                "Only ONE item in_progress at a time. Mark completed immediately when done."
            ),
            func=todo,
            input_model={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Task items to write. Omit to read.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {"type": "string",
                                           "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                    "merge": {"type": "boolean", "default": False,
                              "description": "true: merge updates, false: replace list."},
                },
                "required": [],
            },
        )
