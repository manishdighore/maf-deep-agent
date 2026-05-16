# Sub-Agent Streaming

By default, when the parent agent calls `delegate_task`, the sub-agent runs silently. The parent's `run_stream()` only emits the parent's own tokens — sub-agent tokens are invisible.

This document explains why, and provides two APIs to solve it.

---

## Why Sub-Agent Tokens Don't Surface Automatically

When the framework executes a tool call, it calls `async def delegate_task(...) -> str`. From the framework's perspective this is an opaque async function. It waits for the `str` return value and injects it as a `tool` message. Everything that happens inside — including sub-agent LLM calls — is invisible to the parent stream.

```
parent.run_stream():
  → "I'll research that..."          ← parent token, visible
  → [tool_call: delegate_task]       ← visible (tool call event)
    ... sub-agent runs here ...      ← INVISIBLE, even if streaming internally
  → [tool_result: "## Task 1: ..."] ← visible (tool result)
  → "Based on the research..."       ← parent token, visible
```

This is a fundamental constraint of the agent-framework tool execution model, not a limitation of this package.

---

## High-Level API: `stream_with_subagents()` (Recommended)

One async iterator. Parent tokens and sub-agent tokens merged into a single stream of native `AgentResponseUpdate` objects from `agent-framework`. No custom types, no queue management, no cleanup.

```python
from deep_agent import stream_with_subagents
from agent_framework import AgentResponseUpdate

async for update in stream_with_subagents(agent, "Research quantum computing", session=session):
    print(f"[{update.author_name}] {update.text}")
    if update.finish_reason == "stop":
        print(f"  ↑ {update.author_name} finished")
```

### Distinguishing parent vs sub-agent

All events are `AgentResponseUpdate`. Check `author_name`:

| `author_name` | Meaning |
|----------------|---------|
| Agent's name (e.g. `"maf-deep-agent"`) | Parent token |
| Sub-agent name (e.g. `"python-researcher"`) | Sub-agent token |

Check `finish_reason == "stop"` to detect when a source is done.

### What it handles for you

- Creates a fresh `asyncio.Queue` per call (per user message)
- Sets `active_subagent_queue` ContextVar — sub-agents auto-stream into it
- Runs parent stream + queue drain as concurrent tasks
- Stamps `author_name` on every update for source identification
- On cancellation (`CancelledError`): cancels both tasks, sends `finish_reason="stop"` for sub-agents, resets ContextVar
- On error: same cleanup — no leaked tasks, no orphaned queues
- On completion: resets ContextVar, awaits tasks, garbage collects queue

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent` | `Agent` | *required* | The agent from `create_deep_agent` |
| `message` | `str` | *required* | User message |
| `session` | `AgentSession` | `None` | Session to use |
| `parent_name` | `str` | agent's `name` | Override `author_name` for parent events |

---

## Framework Caveats

### 1. ContextVar is async-safe, not thread-safe
`ContextVar` values are inherited when a new `asyncio.Task` is created from the current context. Since `asyncio.gather()` creates tasks from the current context, parallel sub-agents automatically inherit the same queue — no extra wiring needed.

### 2. Parallel sub-agents write to the same queue — interleaved
With 3 concurrent sub-agents, updates arrive in arrival order, not sub-agent order:

```
[sub:Research Python]  "Python was created..."
[sub:Research Rust]    "Rust is a systems..."
[sub:Research Python]  " by Guido van Rossum..."
[sub:Research Go]      "Go was designed at..."
```

Use `author_name` to route updates to the correct UI panel.

### 3. Sub-agents run inside tool calls — timing
The sub-agent tokens start arriving only when `delegate_task` is actually called by the LLM — which could be mid-stream. The parent may produce several tokens before any sub-agent event appears.

### 4. Cancellation propagation
If the caller cancels (WebSocket disconnect, `CancelledError`), `DelegateTaskProvider` catches it, sends an `AgentResponseUpdate` with `finish_reason="stop"` for the cancelled sub-agent, and re-raises so `asyncio.gather` cancels sibling sub-agents too.

---

## FastAPI WebSocket Example

Using `stream_with_subagents` — the recommended approach:

```python
import json
from fastapi import FastAPI, WebSocket
from deep_agent import create_deep_agent, stream_with_subagents

app = FastAPI()
agent = create_deep_agent(client=client, instructions="...")
sessions: dict[str, object] = {}

@app.websocket("/ws/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()

    if session_id not in sessions:
        sessions[session_id] = agent.create_session(session_id=session_id)
    session = sessions[session_id]

    async for raw in websocket.iter_text():
        msg = json.loads(raw)

        async for event in stream_with_subagents(agent, msg["text"], session=session):
            await websocket.send_json({
                "author": event.author_name,
                "text": event.text,
                "done": event.finish_reason == "stop",
            })

        await websocket.send_json({"author": "system", "done": True})
```

### Wire format the UI receives

```json
{"author": "maf-deep-agent",     "text": "I'll research this..."}
{"author": "python-researcher",  "text": "Python is a...", "done": false}
{"author": "news-analyst",       "text": "Today...",       "done": false}
{"author": "python-researcher",  "text": "",               "done": true}
{"author": "maf-deep-agent",     "text": "Based on the research..."}
{"author": "system",             "done": true}
```

---

## Low-Level API: `active_subagent_queue`

For power users who want custom queue sizing, backpressure, or non-standard drain patterns:

```python
from deep_agent import active_subagent_queue
from agent_framework import AgentResponseUpdate
import asyncio

queue: asyncio.Queue[AgentResponseUpdate | None] = asyncio.Queue(maxsize=100)
token = active_subagent_queue.set(queue)
try:
    # You manage the parent stream + queue drain yourself
    async def stream_parent():
        async for update in agent.run_stream("...", session=session):
            ...
        await queue.put(None)

    async def drain():
        while (item := await queue.get()) is not None:
            # item is AgentResponseUpdate — check item.author_name
            ...

    await asyncio.gather(stream_parent(), drain())
finally:
    active_subagent_queue.reset(token)
```

Each item in the queue is a native `AgentResponseUpdate` with `author_name` set to the sub-agent's name and `finish_reason="stop"` when it's done.

**Caveats with the low-level API:**
- You must drain the queue concurrently with the parent stream (use `asyncio.gather`)
- You must put `None` sentinel after the parent finishes to stop the drain loop
- You must reset the ContextVar in a `finally` block

`stream_with_subagents` handles all of this automatically.

---

## Without WebSockets (CLI / non-streaming)

If you call `agent.run()` (non-streaming), sub-agents also run non-streaming — no queue needed, no changes required. `active_subagent_queue` defaults to `None` and `DelegateTaskProvider` falls back to `child.run()`.

---

## Disable Sub-Agent Streaming

If you don't want streaming for sub-agents even when a queue is set (e.g. for testing), don't set the var:

```python
# Do NOT set active_subagent_queue — sub-agents run silently
response = await agent.run("...", session=session)
```
