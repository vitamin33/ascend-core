"""Memory access audit logging — security audit trail for memory reads/writes.

Logs every memory read and write operation to audit.jsonl for compliance
and security monitoring. Tracks which agents access which memory types.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class MemoryAuditLogger:
    """Append-only audit logger for memory access events."""

    def __init__(self, audit_path: Path) -> None:
        """Initialize with path to audit.jsonl.

        Args:
            audit_path: Path to logs/audit.jsonl.
        """
        self._audit_path = audit_path

    def log_memory_read(
        self,
        agent_id: str,
        memory_type: str,
        project: str = "",
        items_count: int = 0,
        details: str = "",
    ) -> None:
        """Log a memory read operation.

        Args:
            agent_id: The agent performing the read.
            memory_type: Type of memory accessed (exemplars, entities, etc).
            project: Project context for the read.
            items_count: Number of items returned.
            details: Optional details about the access.
        """
        self._write_event({
            "event": "memory_read",
            "agent_id": agent_id,
            "memory_type": memory_type,
            "project": project,
            "items_count": items_count,
            "details": details,
        })

    def log_memory_write(
        self,
        agent_id: str,
        memory_type: str,
        project: str = "",
        key: str = "",
        details: str = "",
    ) -> None:
        """Log a memory write operation.

        Args:
            agent_id: The agent performing the write.
            memory_type: Type of memory written (exemplar, entity, etc).
            project: Project context for the write.
            key: Key or identifier of the written item.
            details: Optional details about the write.
        """
        self._write_event({
            "event": "memory_write",
            "agent_id": agent_id,
            "memory_type": memory_type,
            "project": project,
            "key": key,
            "details": details,
        })

    def log_memory_blocked(
        self,
        agent_id: str,
        memory_type: str,
        reason: str,
        project: str = "",
    ) -> None:
        """Log a blocked memory access (NDA, confidence, validation).

        Args:
            agent_id: The agent whose access was blocked.
            memory_type: Type of memory that was blocked.
            reason: Why the access was blocked.
            project: Project context.
        """
        self._write_event({
            "event": "memory_blocked",
            "agent_id": agent_id,
            "memory_type": memory_type,
            "reason": reason,
            "project": project,
        })

    def _write_event(self, event: dict[str, object]) -> None:
        """Append event to audit log."""
        event["timestamp"] = datetime.now(UTC).isoformat()
        try:
            with open(self._audit_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            logger.warning("Failed to write memory audit event: %s", event)
