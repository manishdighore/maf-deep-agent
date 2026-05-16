"""create_deep_agent — factory function that wires everything together."""

from __future__ import annotations

from typing import Any

from agent_framework import Agent, FunctionTool, InMemoryHistoryProvider
from agent_framework._compaction import SummarizationStrategy
from agent_framework._skills import SkillsProvider

from deep_agent.providers import (
    TrackedCompactionProvider,
    TodoProvider,
    DelegateTaskProvider,
    FilesystemProvider,
    SessionBridgeProvider,
)
from deep_agent.middlewares import (
    SkillToolkitMiddleware,
    SkillToolFilterMiddleware,
    LLMCallLogMiddleware,
    LargeOutputMiddleware,
)
from deep_agent.services.filesystem import ThreadedStateFilesystem


def create_deep_agent(
    *,
    client: Any,
    instructions: str,
    name: str = "maf-deep-agent",
    tools: list[FunctionTool] | None = None,
    skills: list[Any] | None = None,
    skill_toolkits: dict[str, list[FunctionTool]] | None = None,
    target_count: int = 8,
    threshold: int = 12,
    enable_todo: bool = True,
    enable_delegation: bool = True,
    enable_logging: bool = True,
    enable_filesystem: bool = False,
    fs_exclude_tools: set[str] | None = None,
    context_providers: list[Any] | None = None,
    middleware: list[Any] | None = None,
    default_options: dict[str, Any] | None = None,
) -> Agent:
    """Create a batteries-included agent with skills, compaction, todo, and delegation.

    Args:
        client: Chat client (e.g. OpenAIChatClient for Azure/OpenAI).
        instructions: System prompt — 100% user-defined.
        name: Agent name (default "maf-deep-agent").
        tools: Always-on tools added directly to the agent.
        skills: List of InlineSkill (or any SkillResource) for load_skill.
        skill_toolkits: Mutable dict mapping skill names to tool lists.
            Can start with empty lists and be populated later (e.g. after MCP init).
            All providers share this same dict reference.
        target_count: Compaction — keep newest N messages after summarization.
        threshold: Compaction — trigger when non-system msgs > target_count + threshold.
        enable_todo: Include TodoProvider (default True).
        enable_delegation: Include DelegateTaskProvider (default True).
        enable_logging: Include LLMCallLogMiddleware (default True).
        enable_filesystem: Include FilesystemProvider + LargeOutputMiddleware
            (default False).  When enabled, the agent gets session-scoped
            virtual filesystem tools (ls, read_file, write_file, edit_file,
            glob, grep) and large tool outputs are automatically spilled
            to ``/.outputs/`` for on-demand retrieval.
        fs_exclude_tools: Tool names whose output should never be spilled
            by ``LargeOutputMiddleware``.  Defaults to the 6 filesystem tools
            (``ls``, ``read_file``, ``write_file``, ``edit_file``, ``glob``,
            ``grep``) to avoid circular writes.  Pass a custom set to add
            your own exclusions (e.g. tools that return structured data you
            always want inline).
        context_providers: Additional context providers appended AFTER the
            built-in providers. Their ``before_run`` runs last (after delegation),
            and ``after_run`` runs first (reversed order).
        middleware: Additional middleware appended AFTER the built-in
            middleware (SkillToolkitMiddleware, LLMCallLogMiddleware).

    Returns:
        A standard agent_framework.Agent ready for run(), run_stream(), or DevServer.
    """
    if skill_toolkits is None:
        skill_toolkits = {}
    if skills is None:
        skills = []

    # ── Collect all skill tools upfront (flat list across all toolkits) ──
    # Registered on the agent at init so the function-invocation registry always
    # knows how to execute them. SkillToolFilterMiddleware then gates *visibility*
    # to the LLM based on session.state["enabled_toolkits"] per LLM call.
    all_skill_tools: list[FunctionTool] = [
        t for toolkit_tools in skill_toolkits.values() for t in toolkit_tools
    ]
    base_tools: list[FunctionTool] = list(tools or [])
    all_agent_tools: list[FunctionTool] = base_tools + all_skill_tools

    # ── Auto-enrich skill instructions with tool names from skill_toolkits ──
    for skill in skills:
        skill_name = getattr(skill, "name", None)
        if skill_name and skill_name in skill_toolkits:
            tools_list = skill_toolkits[skill_name]
            if tools_list:
                tool_names = ", ".join(t.name for t in tools_list)
                suffix = f"\n\nTools available after loading this skill: {tool_names}"
                current = getattr(skill, "instructions", "") or ""
                if tool_names not in current:
                    skill.instructions = current + suffix

    # ── History ──
    history = InMemoryHistoryProvider(skip_excluded=True)

    # ── Compaction (after_strategy = summarization) ──
    summarization = SummarizationStrategy(
        client=client,
        target_count=target_count,
        threshold=threshold,
    )
    compaction = TrackedCompactionProvider(
        skill_toolkits=skill_toolkits,
        before_strategy=None,
        after_strategy=summarization,
        history_source_id=history.source_id,
    )

    # ── Shared skill state ──
    # Single mutable dict shared by SessionBridgeProvider (hydrates from
    # session.state at turn start), SkillToolkitMiddleware (writes mid-turn
    # after load_skill), and SkillToolFilterMiddleware (reads on every LLM call).
    shared_state: dict[str, Any] = {}

    # ── Context providers ──
    # SessionBridgeProvider MUST be first — it hydrates shared_state from
    # session.state["enabled_toolkits"] so the filter middleware sees
    # previously-loaded skills from the very first LLM call (session reload).
    all_providers: list[Any] = [
        SessionBridgeProvider(shared_state=shared_state),
        history,
        compaction,
    ]
    if skills:
        all_providers.append(SkillsProvider(skills))
    if enable_todo:
        all_providers.append(TodoProvider())
    if enable_delegation:
        all_providers.append(DelegateTaskProvider(
            client=client, skill_toolkits=skill_toolkits, base_tools=tools,
        ))

    # ── Filesystem (optional) ──
    fs: ThreadedStateFilesystem | None = None
    if enable_filesystem:
        fs = ThreadedStateFilesystem()
        all_providers.append(FilesystemProvider(fs=fs))

    if context_providers:
        all_providers.extend(context_providers)

    # ── Middleware ──
    # Order matters:
    #   1. SkillToolkitMiddleware  (FunctionMiddleware) — writes state after load_skill executes
    #   2. SkillToolFilterMiddleware (ChatMiddleware)   — filters tool schema before each LLM call
    #   3. LLMCallLogMiddleware    (ChatMiddleware)     — logs after filter so log shows real tools
    all_middleware: list[Any] = [
        SkillToolkitMiddleware(skill_toolkits=skill_toolkits, shared_state=shared_state),
        SkillToolFilterMiddleware(
            all_tools=all_agent_tools,
            skill_toolkits=skill_toolkits,
            shared_state=shared_state,
        ),
    ]
    if enable_logging:
        all_middleware.append(LLMCallLogMiddleware())
    if enable_filesystem and fs is not None:
        all_middleware.append(LargeOutputMiddleware(
            fs=fs,
            **({"exclude_tools": fs_exclude_tools} if fs_exclude_tools is not None else {}),
        ))
    if middleware:
        all_middleware.extend(middleware)

    return Agent(
        name=name,
        client=client,
        instructions=instructions,
        tools=all_agent_tools,       # base tools + all skill tools registered upfront
        context_providers=all_providers,
        middleware=all_middleware,
        default_options=default_options,
    )
