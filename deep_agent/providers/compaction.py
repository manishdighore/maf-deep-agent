"""TrackedCompactionProvider — compaction with toolkit offloading."""

from __future__ import annotations

import json
from typing import Any

from agent_framework import CompactionProvider, FunctionTool, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    CharacterEstimatorTokenizer,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
)

from deep_agent._logging import agent_log


def _find_loaded_skills(messages: list[Message]) -> set[str]:
    skills: set[str] = set()
    for msg in messages:
        if not hasattr(msg, "contents") or not msg.contents:
            continue
        for content in msg.contents:
            if (
                getattr(content, "type", None) == "function_call"
                and getattr(content, "name", None) == "load_skill"
            ):
                args = getattr(content, "arguments", None)
                if isinstance(args, dict):
                    name = args.get("skill_name", "")
                elif isinstance(args, str):
                    try:
                        name = json.loads(args).get("skill_name", "")
                    except (json.JSONDecodeError, AttributeError):
                        name = ""
                else:
                    name = ""
                if name:
                    skills.add(name)
    return skills


class TrackedCompactionProvider(CompactionProvider):
    """CompactionProvider that offloads toolkits when their load_skill
    call gets compacted away from stored context."""

    def __init__(self, skill_toolkits: dict[str, list[FunctionTool]], **kwargs):
        super().__init__(**kwargs)
        self.skill_toolkits = skill_toolkits

    async def before_run(self, *, agent, session, context, state):
        all_messages = context.get_messages()
        tokenizer = self.tokenizer or CharacterEstimatorTokenizer()
        annotate_message_groups(all_messages)
        annotate_token_counts(all_messages, tokenizer=tokenizer)
        tokens = included_token_count(all_messages)
        agent_log("CompactionProvider", "context",
                  f"{len(all_messages)} msgs, est_tokens: {tokens:,}")
        self._skills_before = _find_loaded_skills(all_messages)
        await super().before_run(agent=agent, session=session, context=context, state=state)

    async def after_run(self, *, agent, session, context, state):
        tokenizer = self.tokenizer or CharacterEstimatorTokenizer()

        history_state = session.state.get(self.history_source_id)
        if isinstance(history_state, dict):
            stored = history_state.get("messages", [])
            msg_count_before = len(stored)
            excluded_before = sum(1 for m in stored if m.additional_properties.get(EXCLUDED_KEY, False))
            annotate_message_groups(stored)
            annotate_token_counts(stored, tokenizer=tokenizer)
            tokens_before = included_token_count(stored)
        else:
            msg_count_before = 0
            excluded_before = 0
            tokens_before = 0

        await super().after_run(agent=agent, session=session, context=context, state=state)

        if not isinstance(history_state, dict):
            return
        stored_after = history_state.get("messages", [])
        msg_count_after = len(stored_after)
        excluded_after = sum(1 for m in stored_after if m.additional_properties.get(EXCLUDED_KEY, False))
        newly_excluded = excluded_after - excluded_before

        if newly_excluded > 0:
            annotate_message_groups(stored_after)
            annotate_token_counts(stored_after, tokenizer=tokenizer)
            tokens_after = included_token_count(stored_after)
            tokens_saved = tokens_before - tokens_after
            non_excluded = msg_count_after - excluded_after

            for m in stored_after:
                if m.additional_properties.get(EXCLUDED_KEY, False):
                    continue
                group_ann = m.additional_properties.get(GROUP_ANNOTATION_KEY)
                if isinstance(group_ann, dict) and group_ann.get(SUMMARY_OF_MESSAGE_IDS_KEY):
                    summary_text = ""
                    if hasattr(m, "contents") and m.contents:
                        for c in m.contents:
                            if isinstance(c, str):
                                summary_text += c
                            elif hasattr(c, "text"):
                                summary_text += (getattr(c, "text", "") or "")
                    if summary_text:
                        preview = summary_text[:600] + ("…" if len(summary_text) > 600 else "")
                        agent_log("CompactionProvider", "summary", f"[dim]{preview}[/dim]")

            agent_log("CompactionProvider", "compacted",
                      f"{newly_excluded} msgs summarized "
                      f"({msg_count_before}→{non_excluded} active)  "
                      f"tokens: [bold green]{tokens_before:,}→{tokens_after:,}[/] "
                      f"([bold green]saved {tokens_saved:,}[/])")

            active_messages = [m for m in stored_after if not m.additional_properties.get(EXCLUDED_KEY, False)]
            skills_after = _find_loaded_skills(active_messages)
            skills_before = getattr(self, "_skills_before", set())
            compacted_away = skills_before - skills_after

            if compacted_away:
                enabled: set[str] = session.state.get("enabled_toolkits", set())
                for skill_name in compacted_away:
                    if skill_name in enabled:
                        enabled.discard(skill_name)
                        tool_names = [t.name for t in self.skill_toolkits.get(skill_name, [])]
                        agent_log("CompactionProvider", "toolkit_offloaded",
                                  f"'{skill_name}' compacted away → {len(tool_names)} tools removed")
        else:
            excluded_count = sum(1 for m in stored_after if m.additional_properties.get(EXCLUDED_KEY, False))
            active_count = msg_count_after - excluded_count
            agent_log("CompactionProvider", "no_compaction",
                      f"{msg_count_after} stored ({active_count} active, {excluded_count} excluded), threshold not reached")
