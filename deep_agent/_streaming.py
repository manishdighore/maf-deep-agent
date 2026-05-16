"""Sub-agent streaming support using ``AgentResponseUpdate`` from agent-framework.

All events — parent and sub-agent — are native ``AgentResponseUpdate`` objects.
Consumers distinguish sources via ``update.author_name``:

* Parent tokens carry ``author_name`` equal to the agent's name (default
  ``"maf-deep-agent"``).
* Sub-agent tokens carry the sub-agent's name chosen by the LLM (e.g.
  ``"python-researcher"``).
* ``finish_reason="stop"`` indicates the source has finished.

Provides two APIs:

1. **High-level** (recommended): ``stream_with_subagents()`` — a single async
   iterator that merges parent and sub-agent tokens into one stream of
   ``AgentResponseUpdate`` objects.

2. **Low-level**: ``active_subagent_queue`` ContextVar — for power users who
   want custom queue sizing, backpressure, or non-standard drain patterns.

Usage (high-level)::

    from deep_agent import stream_with_subagents

    async for update in stream_with_subagents(agent, "Hello", session=session):
        print(f"[{update.author_name}] {update.text}")

Usage (low-level)::

    from deep_agent import active_subagent_queue
    from agent_framework import AgentResponseUpdate
    import asyncio

    queue: asyncio.Queue[AgentResponseUpdate | None] = asyncio.Queue()
    token = active_subagent_queue.set(queue)
    try:
        async for update in agent.run_stream(...):
            ...
    finally:
        active_subagent_queue.reset(token)
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, AsyncIterator

from agent_framework import AgentResponseUpdate


# Low-level API — set this ContextVar before calling agent.run_stream()
# to receive real-time sub-agent tokens. Reset it in a finally block.
active_subagent_queue: ContextVar[asyncio.Queue[AgentResponseUpdate | None] | None] = ContextVar(
    "active_subagent_queue", default=None
)


async def stream_with_subagents(
    agent: Any,
    message: str,
    *,
    session: Any = None,
    parent_name: str | None = None,
) -> AsyncIterator[AgentResponseUpdate]:
    """Iterate parent tokens and sub-agent tokens as a single unified stream.

    Every yielded object is a native ``AgentResponseUpdate``.  Use
    ``update.author_name`` to distinguish parent vs sub-agent tokens, and
    ``update.finish_reason == "stop"`` to detect when a source is done.

    Args:
        agent: The agent created by ``create_deep_agent``.
        message: The user message to send.
        session: The ``AgentSession`` to use.
        parent_name: Override ``author_name`` for parent events.  Defaults to
            the agent's ``name`` attribute (usually ``"maf-deep-agent"``).

    Yields:
        ``AgentResponseUpdate`` objects in arrival order.

    Example::

        async for update in stream_with_subagents(agent, "Research AI", session=s):
            await ws.send_json({
                "author": update.author_name,
                "text": update.text,
                "done": update.finish_reason == "stop",
            })
    """
    source = parent_name or getattr(agent, "name", "agent")
    queue: asyncio.Queue[AgentResponseUpdate | None] = asyncio.Queue()
    var_token = active_subagent_queue.set(queue)

    # Merged output channel — both tasks write here, caller reads from it
    out: asyncio.Queue[AgentResponseUpdate | None] = asyncio.Queue()

    async def _stream_parent() -> None:
        try:
            async for update in agent.run_stream(message, session=session):
                # Stamp author_name so consumer can identify the parent
                update.author_name = source
                await out.put(update)
            await out.put(AgentResponseUpdate(author_name=source, finish_reason="stop"))
        except asyncio.CancelledError:
            await out.put(AgentResponseUpdate(author_name=source, finish_reason="stop"))
            raise
        finally:
            # Signal sub-agent drain to stop
            await queue.put(None)

    async def _drain_subagents() -> None:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                await out.put(item)
        except asyncio.CancelledError:
            pass

    parent_task = asyncio.create_task(_stream_parent())
    drain_task = asyncio.create_task(_drain_subagents())

    try:
        # Both tasks are running — yield events as they arrive
        while True:
            # Check if both producers are done
            if parent_task.done() and drain_task.done() and out.empty():
                break
            try:
                event = await asyncio.wait_for(out.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # No event ready — check if tasks are still alive
                if parent_task.done() and drain_task.done():
                    break
                continue
            if event is None:
                break
            yield event
    except asyncio.CancelledError:
        parent_task.cancel()
        drain_task.cancel()
        raise
    finally:
        # Ensure tasks are cleaned up
        if not parent_task.done():
            parent_task.cancel()
        if not drain_task.done():
            drain_task.cancel()
        # Suppress CancelledError from our own tasks
        for task in (parent_task, drain_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        active_subagent_queue.reset(var_token)

