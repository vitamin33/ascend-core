"""Workflow blackboard — shared state for multi-agent pipeline coordination.

Agents in a workflow pipeline read/write to a shared JSON blackboard.
Each workflow gets its own blackboard file in data/blackboard/{workflow_id}.json.
Supports concurrent reads, append-only contributions, and TTL-based expiry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 48


@dataclass
class BlackboardContribution:
    """A single agent's contribution to the blackboard."""

    agent_id: str
    key: str
    value: object
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    confidence: float = 0.8


@dataclass
class Blackboard:
    """Shared state for a workflow pipeline."""

    workflow_id: str
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    ttl_hours: float = _DEFAULT_TTL_HOURS
    contributions: list[BlackboardContribution] = field(default_factory=list)
    status: str = "active"  # active, completed, expired

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return {
            "workflow_id": self.workflow_id,
            "created_at": self.created_at,
            "ttl_hours": self.ttl_hours,
            "status": self.status,
            "contributions": [asdict(c) for c in self.contributions],
        }

    def get_latest(self, key: str) -> object | None:
        """Get the most recent value for a key.

        Args:
            key: The key to look up.

        Returns:
            The most recent value or None.
        """
        for contrib in reversed(self.contributions):
            if contrib.key == key:
                return contrib.value
        return None

    def get_by_agent(self, agent_id: str) -> list[BlackboardContribution]:
        """Get all contributions from a specific agent.

        Args:
            agent_id: The agent to filter by.

        Returns:
            List of contributions from that agent.
        """
        return [c for c in self.contributions if c.agent_id == agent_id]

    def get_all_keys(self) -> dict[str, object]:
        """Get latest value for each key.

        Returns:
            Dict mapping key to latest value.
        """
        result: dict[str, object] = {}
        for contrib in self.contributions:
            result[contrib.key] = contrib.value
        return result


class BlackboardStore:
    """Manage workflow blackboards in data/blackboard/."""

    def __init__(self, blackboard_dir: Path) -> None:
        """Initialize blackboard store.

        Args:
            blackboard_dir: Path to data/blackboard/ directory.
        """
        self._dir = blackboard_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        workflow_id: str,
        ttl_hours: float = _DEFAULT_TTL_HOURS,
    ) -> Blackboard:
        """Create a new blackboard for a workflow.

        Args:
            workflow_id: Unique workflow identifier.
            ttl_hours: Time-to-live in hours.

        Returns:
            New Blackboard instance.
        """
        board = Blackboard(workflow_id=workflow_id, ttl_hours=ttl_hours)
        self._save(board)
        return board

    def get(self, workflow_id: str) -> Blackboard | None:
        """Load a blackboard by workflow ID.

        Args:
            workflow_id: The workflow identifier.

        Returns:
            Blackboard or None if not found.
        """
        path = self._path(workflow_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            board = self._from_dict(data)
            if self._is_expired(board):
                board.status = "expired"
                self._save(board)
            return board
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Failed to load blackboard %s: %s", workflow_id, exc)
            return None

    def contribute(
        self,
        workflow_id: str,
        agent_id: str,
        key: str,
        value: object,
        confidence: float = 0.8,
    ) -> bool:
        """Add a contribution to a blackboard.

        Args:
            workflow_id: The workflow identifier.
            agent_id: The contributing agent.
            key: Data key.
            value: Data value (must be JSON-serializable).
            confidence: Confidence score (0-1).

        Returns:
            True if contribution was added, False if blackboard not found/expired.
        """
        board = self.get(workflow_id)
        if board is None or board.status != "active":
            return False
        contrib = BlackboardContribution(
            agent_id=agent_id,
            key=key,
            value=value,
            confidence=confidence,
        )
        board.contributions.append(contrib)
        self._save(board)
        return True

    def complete(self, workflow_id: str) -> bool:
        """Mark a blackboard as completed.

        Args:
            workflow_id: The workflow identifier.

        Returns:
            True if marked, False if not found.
        """
        board = self.get(workflow_id)
        if board is None:
            return False
        board.status = "completed"
        self._save(board)
        return True

    def get_for_injection(
        self,
        workflow_id: str,
        max_entries: int = 10,
    ) -> str:
        """Get blackboard context for injection into agent prompts.

        Args:
            workflow_id: The workflow identifier.
            max_entries: Maximum contributions to include.

        Returns:
            Formatted context string or empty string.
        """
        board = self.get(workflow_id)
        if board is None or not board.contributions:
            return ""

        recent = board.contributions[-max_entries:]
        parts: list[str] = ["## Workflow Blackboard"]
        for contrib in recent:
            val_str = json.dumps(contrib.value) if not isinstance(
                contrib.value, str,
            ) else contrib.value
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            parts.append(
                f"- [{contrib.agent_id}] {contrib.key}: {val_str}"
            )
        return "\n".join(parts)

    def cleanup_expired(self) -> int:
        """Remove expired blackboard files.

        Returns:
            Number of files removed.
        """
        removed = 0
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                board = self._from_dict(data)
                if self._is_expired(board):
                    path.unlink()
                    removed += 1
            except (json.JSONDecodeError, OSError, KeyError):
                continue
        return removed

    def list_active(self) -> list[str]:
        """List all active workflow IDs.

        Returns:
            List of active workflow IDs.
        """
        active: list[str] = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("status") == "active":
                    wid = data.get("workflow_id", path.stem)
                    active.append(str(wid))
            except (json.JSONDecodeError, OSError):
                continue
        return active

    def _path(self, workflow_id: str) -> Path:
        """Get file path for a workflow blackboard."""
        safe_id = workflow_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe_id}.json"

    def _save(self, board: Blackboard) -> None:
        """Persist blackboard to disk."""
        path = self._path(board.workflow_id)
        path.write_text(json.dumps(board.to_dict(), indent=2))

    @staticmethod
    def _from_dict(data: dict[str, object]) -> Blackboard:
        """Deserialize blackboard from dict."""
        contribs_raw = data.get("contributions", [])
        contribs: list[BlackboardContribution] = []
        if isinstance(contribs_raw, list):
            for c in contribs_raw:
                if isinstance(c, dict):
                    contribs.append(BlackboardContribution(
                        agent_id=str(c.get("agent_id", "")),
                        key=str(c.get("key", "")),
                        value=c.get("value"),
                        timestamp=str(c.get("timestamp", "")),
                        confidence=float(str(c.get("confidence", 0.8))),
                    ))
        return Blackboard(
            workflow_id=str(data.get("workflow_id", "")),
            created_at=str(data.get("created_at", "")),
            ttl_hours=float(str(data.get("ttl_hours", _DEFAULT_TTL_HOURS))),
            contributions=contribs,
            status=str(data.get("status", "active")),
        )

    @staticmethod
    def _is_expired(board: Blackboard) -> bool:
        """Check if blackboard has exceeded its TTL."""
        try:
            created = datetime.fromisoformat(board.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_hours = (
                datetime.now(UTC) - created
            ).total_seconds() / 3600
            return age_hours > board.ttl_hours
        except (ValueError, TypeError):
            return False
