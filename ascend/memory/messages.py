"""Agent-to-agent message queue with TTL.

Enables async cross-agent communication:
Agent A leaves a note for Agent B, context builder injects it.
Messages auto-expire after TTL (default 48h).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 48


@dataclass
class AgentMessage:
    """A message from one agent to another."""

    from_agent: str
    to_agent: str
    content: str
    priority: str  # "high", "normal", "low"
    created_at: str
    expires_at: str
    read: bool = False

    def is_expired(self) -> bool:
        """Check if message has passed its TTL."""
        try:
            expires = datetime.fromisoformat(self.expires_at)
            return datetime.now(UTC) > expires
        except (ValueError, TypeError):
            return True

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AgentMessage:
        """Deserialize from dict."""
        return cls(
            from_agent=str(data.get("from_agent", "")),
            to_agent=str(data.get("to_agent", "")),
            content=str(data.get("content", "")),
            priority=str(data.get("priority", "normal")),
            created_at=str(data.get("created_at", "")),
            expires_at=str(data.get("expires_at", "")),
            read=bool(data.get("read", False)),
        )


class AgentMessageQueue:
    """Per-agent JSONL message queue with TTL expiration.

    Messages stored in data/messages/{agent_id}.jsonl.
    Expired messages cleaned up on read.
    """

    def __init__(
        self,
        base_dir: Path,
        ttl_hours: int = _DEFAULT_TTL_HOURS,
    ) -> None:
        """Initialize message queue.

        Args:
            base_dir: Root directory for message files (data/messages/).
            ttl_hours: Default time-to-live in hours for messages.
        """
        self._base_dir = base_dir
        self._ttl_hours = ttl_hours

    def _agent_file(self, agent_type: str) -> Path:
        """Get JSONL file path for an agent's inbox."""
        return self._base_dir / f"{agent_type}.jsonl"

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        priority: str = "normal",
        ttl_hours: int | None = None,
    ) -> AgentMessage:
        """Send a message from one agent to another.

        Args:
            from_agent: Sending agent type.
            to_agent: Receiving agent type.
            content: Message content.
            priority: Message priority (high/normal/low).
            ttl_hours: Override default TTL.

        Returns:
            The created message.
        """
        self._base_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        ttl = ttl_hours if ttl_hours is not None else self._ttl_hours
        expires = now + timedelta(hours=ttl)

        msg = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            priority=priority,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )

        path = self._agent_file(to_agent)
        with open(path, "a") as f:
            f.write(json.dumps(msg.to_dict()) + "\n")

        logger.info(
            "Message sent: %s -> %s (%s)",
            from_agent, to_agent, priority,
        )
        return msg

    def read_unread(self, agent_type: str) -> list[AgentMessage]:
        """Read all unread, non-expired messages for an agent.

        Also cleans up expired messages.

        Args:
            agent_type: Agent to read messages for.

        Returns:
            List of unread messages, highest priority first.
        """
        path = self._agent_file(agent_type)
        if not path.exists():
            return []

        messages: list[AgentMessage] = []
        keep: list[AgentMessage] = []

        try:
            for line in path.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                msg = AgentMessage.from_dict(json.loads(line))
                if msg.is_expired():
                    continue  # Drop expired
                keep.append(msg)
                if not msg.read:
                    messages.append(msg)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read messages for %s: %s", agent_type, exc)
            return []

        # Rewrite file without expired messages, mark read
        for msg in keep:
            msg.read = True
        self._rewrite(agent_type, keep)

        # Sort: high > normal > low
        priority_order = {"high": 0, "normal": 1, "low": 2}
        messages.sort(key=lambda m: priority_order.get(m.priority, 1))
        return messages

    def _rewrite(self, agent_type: str, messages: list[AgentMessage]) -> None:
        """Rewrite agent's message file with given messages."""
        path = self._agent_file(agent_type)
        if not messages:
            if path.exists():
                path.unlink()
            return
        with open(path, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg.to_dict()) + "\n")

    def get_for_injection(self, agent_type: str) -> str:
        """Get formatted unread messages for injection into agent prompt.

        Args:
            agent_type: Agent to get messages for.

        Returns:
            Formatted messages section or empty string.
        """
        messages = self.read_unread(agent_type)
        if not messages:
            return ""

        parts: list[str] = ["## Messages From Other Agents"]
        for msg in messages:
            entry = (
                f"\nFrom: {msg.from_agent} "
                f"(priority: {msg.priority})\n"
                f"{msg.content}"
            )
            parts.append(entry)

        return "\n".join(parts)

    def cleanup_expired(self) -> int:
        """Remove all expired messages across all agents.

        Returns:
            Number of expired messages removed.
        """
        if not self._base_dir.exists():
            return 0

        removed = 0
        for path in self._base_dir.glob("*.jsonl"):
            try:
                messages: list[AgentMessage] = []
                for line in path.read_text().strip().split("\n"):
                    if not line.strip():
                        continue
                    msg = AgentMessage.from_dict(json.loads(line))
                    if msg.is_expired():
                        removed += 1
                    else:
                        messages.append(msg)
                agent_type = path.stem
                self._rewrite(agent_type, messages)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to cleanup %s: %s", path.name, exc)

        if removed > 0:
            logger.info("Cleaned up %d expired messages", removed)
        return removed
