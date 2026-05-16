"""Smoke tests — verify the package imports and factory signature."""

from deep_agent import create_deep_agent, stream_with_subagents, active_subagent_queue


def test_public_api_imports():
    """All public symbols are importable."""
    assert callable(create_deep_agent)
    assert callable(stream_with_subagents)
    assert active_subagent_queue is not None


def test_create_deep_agent_rejects_missing_client():
    """Factory requires client and instructions."""
    import pytest
    with pytest.raises(TypeError):
        create_deep_agent()  # type: ignore[call-arg]
