"""deep_agent — Batteries-included agent builder with skills, compaction, todo, and delegation."""

from deep_agent._builder import create_deep_agent
from deep_agent._streaming import (
    active_subagent_queue,
    stream_with_subagents,
)

__all__ = [
    "create_deep_agent",
    "stream_with_subagents",
    "active_subagent_queue",
]
