"""NDA boundary enforcement for cross-project memory isolation.

Loads redaction rules from config/nda_boundaries.yaml and applies them
before injecting project context into agent prompts. Prevents client
data leakage across NDA-protected project boundaries.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "nda_boundaries.yaml"


@dataclass(frozen=True)
class RedactionRule:
    """A single regex-based redaction rule."""

    pattern: re.Pattern[str]
    replacement: str


@dataclass
class ProjectBoundary:
    """NDA boundary config for one project."""

    project: str
    nda_protected: bool
    shareable: list[str] | str
    confidential: list[str]
    redaction_rules: list[RedactionRule] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryAccessEvent:
    """Audit event for memory access tracking."""

    event: str  # memory_read, cross_project_blocked, redaction_applied
    task_id: str
    project: str
    details: dict[str, object]


class NdaBoundary:
    """Enforce NDA boundaries across project memory."""

    def __init__(self, config_path: Path = _DEFAULT_CONFIG) -> None:
        """Initialize with path to nda_boundaries.yaml.

        Args:
            config_path: Path to NDA boundaries config file.
        """
        self._config_path = config_path
        self._boundaries: dict[str, ProjectBoundary] = {}
        self._audit_events: list[MemoryAccessEvent] = []
        self._load_config()

    def _load_config(self) -> None:
        """Load NDA boundaries from YAML config."""
        if not self._config_path.exists():
            logger.warning("NDA config not found: %s", self._config_path)
            return
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return
            for project, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                rules: list[RedactionRule] = []
                for rule in cfg.get("redaction_rules", []) or []:
                    if isinstance(rule, dict) and "pattern" in rule:
                        try:
                            compiled = re.compile(rule["pattern"])
                            rules.append(RedactionRule(
                                pattern=compiled,
                                replacement=rule.get("replacement", "[REDACTED]"),
                            ))
                        except re.error as exc:
                            logger.warning(
                                "Invalid regex in NDA config %s: %s",
                                project, exc,
                            )
                shareable = cfg.get("shareable", [])
                self._boundaries[project] = ProjectBoundary(
                    project=project,
                    nda_protected=bool(cfg.get("nda_protected", False)),
                    shareable=shareable if shareable else [],
                    confidential=cfg.get("confidential", []) or [],
                    redaction_rules=rules,
                )
            logger.info(
                "Loaded NDA boundaries: %d projects", len(self._boundaries),
            )
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to load NDA config: %s", exc)

    def is_nda_protected(self, project: str) -> bool:
        """Check if a project is NDA-protected.

        Args:
            project: Project name.

        Returns:
            True if project has NDA protection enabled.
        """
        boundary = self._boundaries.get(project)
        if boundary is None:
            return False
        return boundary.nda_protected

    def can_access_cross_project(
        self,
        source_project: str,
        target_project: str,
        task_id: str = "",
    ) -> bool:
        """Check if data from source_project can be shared with target_project.

        Args:
            source_project: Project where data originates.
            target_project: Project requesting the data.
            task_id: Task ID for audit logging.

        Returns:
            True if access is allowed (possibly with redaction).
        """
        if source_project == target_project:
            return True
        source_boundary = self._boundaries.get(source_project)
        if source_boundary is None or not source_boundary.nda_protected:
            return True
        # NDA-protected source: block direct access
        self._audit_events.append(MemoryAccessEvent(
            event="cross_project_blocked",
            task_id=task_id,
            project=target_project,
            details={
                "attempted_source": source_project,
                "reason": "nda_boundary",
            },
        ))
        return False

    def redact_text(
        self,
        text: str,
        project: str,
        task_id: str = "",
    ) -> str:
        """Apply redaction rules for a project to text content.

        Args:
            text: Text to redact.
            project: Project whose redaction rules to apply.
            task_id: Task ID for audit logging.

        Returns:
            Redacted text.
        """
        boundary = self._boundaries.get(project)
        if boundary is None or not boundary.redaction_rules:
            return text
        changes = 0
        result = text
        for rule in boundary.redaction_rules:
            new_result = rule.pattern.sub(rule.replacement, result)
            if new_result != result:
                changes += 1
                result = new_result
        if changes > 0:
            self._audit_events.append(MemoryAccessEvent(
                event="redaction_applied",
                task_id=task_id,
                project=project,
                details={
                    "rules_matched": changes,
                },
            ))
        return result

    def get_boundary(self, project: str) -> ProjectBoundary | None:
        """Get the NDA boundary config for a project.

        Args:
            project: Project name.

        Returns:
            ProjectBoundary or None if not configured.
        """
        return self._boundaries.get(project)

    def drain_audit_events(self) -> list[MemoryAccessEvent]:
        """Return and clear pending audit events.

        Returns:
            List of memory access audit events.
        """
        events = list(self._audit_events)
        self._audit_events.clear()
        return events

    def write_audit_events(self, audit_path: Path) -> int:
        """Write pending audit events to JSONL file.

        Args:
            audit_path: Path to audit.jsonl.

        Returns:
            Number of events written.
        """
        events = self.drain_audit_events()
        if not events:
            return 0
        try:
            with open(audit_path, "a") as f:
                for evt in events:
                    entry = {
                        "event": evt.event,
                        "task_id": evt.task_id,
                        "project": evt.project,
                        **evt.details,
                    }
                    f.write(json.dumps(entry) + "\n")
            return len(events)
        except OSError:
            logger.warning("Failed to write NDA audit events")
            return 0
