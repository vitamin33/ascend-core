"""Failure case store for agent episodic memory.

Stores structured failure records so agents can learn from past mistakes.
Context builder injects relevant failures as "avoid these patterns" context.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CASES = 10
_DEFAULT_INJECT_COUNT = 3


@dataclass
class FailureCase:
    """A structured record of an agent failure."""

    task_description: str
    approach_taken: str
    error: str
    root_cause: str
    fix_applied: str
    prevented_future: bool
    recorded_at: str
    agent_type: str
    source_task_id: str

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FailureCase:
        """Deserialize from dict."""
        return cls(
            task_description=str(data.get("task_description", "")),
            approach_taken=str(data.get("approach_taken", "")),
            error=str(data.get("error", "")),
            root_cause=str(data.get("root_cause", "")),
            fix_applied=str(data.get("fix_applied", "")),
            prevented_future=bool(data.get("prevented_future", False)),
            recorded_at=str(data.get("recorded_at", "")),
            agent_type=str(data.get("agent_type", "")),
            source_task_id=str(data.get("source_task_id", "")),
        )


class FailureCaseStore:
    """Manages per-agent failure case storage.

    Keeps last N failures per agent. Oldest are evicted when at capacity.
    """

    def __init__(
        self,
        base_dir: Path,
        max_cases: int = _DEFAULT_MAX_CASES,
    ) -> None:
        """Initialize failure case store.

        Args:
            base_dir: Root directory for failure storage (data/episodes/failures/).
            max_cases: Maximum failure cases to keep per agent.
        """
        self._base_dir = base_dir
        self._max_cases = max_cases

    def _agent_dir(self, agent_type: str) -> Path:
        """Get directory for an agent's failure cases."""
        return self._base_dir / agent_type

    def save(self, failure: FailureCase) -> Path:
        """Save a failure case.

        Args:
            failure: Failure case to save.

        Returns:
            Path to the saved file.
        """
        agent_dir = self._agent_dir(failure.agent_type)
        agent_dir.mkdir(parents=True, exist_ok=True)

        existing = sorted(agent_dir.glob("fail_*.json"))

        # Evict oldest if at capacity
        while len(existing) >= self._max_cases:
            oldest = existing.pop(0)
            oldest.unlink()
            logger.info("Evicted old failure case: %s", oldest.name)

        # Determine next index
        idx = 1
        if existing:
            last_name = existing[-1].stem  # e.g. "fail_005"
            try:
                idx = int(last_name.split("_")[1]) + 1
            except (ValueError, IndexError):
                idx = len(existing) + 1

        path = agent_dir / f"fail_{idx:03d}.json"
        path.write_text(json.dumps(failure.to_dict(), indent=2))
        logger.info(
            "Saved failure case %d for %s: %s",
            idx, failure.agent_type, failure.error[:80],
        )
        return path

    def load(self, agent_type: str) -> list[FailureCase]:
        """Load all failure cases for an agent, newest first.

        Args:
            agent_type: Agent identifier.

        Returns:
            List of failure cases, most recent first.
        """
        agent_dir = self._agent_dir(agent_type)
        if not agent_dir.exists():
            return []

        cases: list[FailureCase] = []
        for path in sorted(agent_dir.glob("fail_*.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
                cases.append(FailureCase.from_dict(data))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load failure case %s: %s", path, exc)

        return cases

    def get_for_injection(
        self,
        agent_type: str,
        count: int = _DEFAULT_INJECT_COUNT,
    ) -> str:
        """Get formatted failure cases for injection into agent prompt.

        Args:
            agent_type: Agent identifier.
            count: Number of failure cases to inject.

        Returns:
            Formatted failure cases section or empty string.
        """
        cases = self.load(agent_type)[:count]
        if not cases:
            return ""

        parts: list[str] = ["## Avoid These Past Mistakes"]
        for i, case in enumerate(cases, 1):
            entry = (
                f"\n### Failure {i}\n"
                f"Task: {case.task_description}\n"
                f"Error: {case.error}\n"
                f"Root cause: {case.root_cause}"
            )
            if case.fix_applied:
                entry += f"\nFix: {case.fix_applied}"
            parts.append(entry)

        return "\n".join(parts)

    def create_failure(
        self,
        agent_type: str,
        task_description: str,
        error: str,
        task_id: str,
        approach_taken: str = "",
        root_cause: str = "",
        fix_applied: str = "",
    ) -> FailureCase:
        """Create a FailureCase instance with current timestamp.

        Args:
            agent_type: Agent that failed.
            task_description: What the task was.
            error: Error message or description.
            task_id: Source task ID.
            approach_taken: What approach was tried.
            root_cause: Why it failed.
            fix_applied: What was done to fix it.

        Returns:
            New FailureCase instance.
        """
        return FailureCase(
            task_description=task_description,
            approach_taken=approach_taken,
            error=error,
            root_cause=root_cause,
            fix_applied=fix_applied,
            prevented_future=False,
            recorded_at=datetime.now(UTC).isoformat(),
            agent_type=agent_type,
            source_task_id=task_id,
        )
