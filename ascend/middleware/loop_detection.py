"""Doom loop prevention middleware for agent actions."""

from __future__ import annotations

import hashlib
import time
from collections import deque


class LoopDetectionMiddleware:
    """Detect when an agent repeats similar actions in a sliding time window."""

    def __init__(
        self, max_similar: int = 5, window_minutes: int = 30
    ) -> None:
        """Initialize loop detector.

        Args:
            max_similar: Number of similar actions before warning.
            window_minutes: Sliding window size in minutes.
        """
        self._max_similar = max_similar
        self._window_seconds = window_minutes * 60
        self._history: dict[str, deque[tuple[str, float]]] = {}
        self._time_fn = time.monotonic

    def on_agent_action(
        self, agent_id: str, action: dict[str, str], context: dict[str, str]
    ) -> str | None:
        """Check if the agent is stuck in a loop.

        Args:
            agent_id: Unique agent identifier.
            action: Action dict with "type", "target", optional "operation".
            context: Additional context (unused in stub).

        Returns:
            Warning string if loop detected, None otherwise.
        """
        sig = self._compute_signature(action)
        now = self._time_fn()

        if agent_id not in self._history:
            self._history[agent_id] = deque()

        window = self._history[agent_id]
        self._prune_window(window, now)
        window.append((sig, now))

        similar_count = sum(1 for s, _ in window if s == sig)
        if similar_count >= self._max_similar:
            return (
                f"Loop detected: agent '{agent_id}' performed "
                f"{similar_count} similar actions "
                f"({action.get('type', '?')}:{action.get('target', '?')}) "
                f"in the last {self._window_seconds // 60} minutes"
            )
        return None

    def _compute_signature(self, action: dict[str, str]) -> str:
        """Compute MD5 hash of action type + target + operation.

        Args:
            action: Action dict.

        Returns:
            Hex digest string.
        """
        raw = (
            action.get("type", "")
            + action.get("target", "")
            + action.get("operation", "")
        )
        return hashlib.md5(raw.encode()).hexdigest()

    def _prune_window(
        self, window: deque[tuple[str, float]], now: float
    ) -> None:
        """Remove entries older than the sliding window.

        Args:
            window: Deque of (signature, timestamp) pairs.
            now: Current monotonic time.
        """
        cutoff = now - self._window_seconds
        while window and window[0][1] < cutoff:
            window.popleft()

    def reset(self, agent_id: str) -> None:
        """Clear history for a specific agent.

        Args:
            agent_id: Agent whose history to clear.
        """
        self._history.pop(agent_id, None)
