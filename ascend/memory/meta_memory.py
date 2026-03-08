"""Meta-memory — memory about memory.

Confidence gating, coverage map, and staleness tracking.
Provides system-wide awareness of what the agent fleet knows and doesn't know.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Confidence thresholds
CONFIDENCE_BLOCK_THRESHOLD = 0.4
CONFIDENCE_WARN_THRESHOLD = 0.6


@dataclass
class CoverageEntry:
    """Knowledge coverage for a single project."""

    project: str
    known: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)


@dataclass
class StalenessEntry:
    """Staleness status for a memory source."""

    name: str
    source_type: str  # snapshot, database, file
    age_hours: float
    status: str  # fresh, aging, stale, critical
    last_updated: str


class ConfidenceGate:
    """Gate memory injection based on confidence scores.

    Confidence scale:
    - 1.0 = Verified by human or deterministic check
    - 0.8-0.9 = High confidence from reliable agent
    - 0.5-0.7 = Medium confidence from LLM-generated content
    - 0.3-0.4 = Low confidence, needs verification
    - <0.3 = Unreliable, should not be injected
    """

    def __init__(
        self,
        block_threshold: float = CONFIDENCE_BLOCK_THRESHOLD,
        warn_threshold: float = CONFIDENCE_WARN_THRESHOLD,
    ) -> None:
        """Initialize with confidence thresholds.

        Args:
            block_threshold: Below this, memory is never injected.
            warn_threshold: Below this, memory gets a warning label.
        """
        self._block = block_threshold
        self._warn = warn_threshold

    def should_inject(self, confidence: float) -> bool:
        """Check if memory with given confidence should be injected.

        Args:
            confidence: Confidence score (0-1).

        Returns:
            True if confidence is above block threshold.
        """
        return confidence >= self._block

    def label(self, confidence: float) -> str:
        """Get confidence label for display.

        Args:
            confidence: Confidence score (0-1).

        Returns:
            Label string: high, medium, low, or blocked.
        """
        if confidence < self._block:
            return "blocked"
        if confidence < self._warn:
            return "low"
        if confidence < 0.8:
            return "medium"
        return "high"

    def filter_by_confidence(
        self,
        items: list[tuple[float, str]],
    ) -> list[tuple[float, str]]:
        """Filter items by confidence threshold.

        Args:
            items: List of (confidence, content) tuples.

        Returns:
            Filtered list with only items above block threshold.
        """
        return [
            (conf, content)
            for conf, content in items
            if conf >= self._block
        ]


class CoverageMap:
    """Track what the system knows and doesn't know per project."""

    def __init__(self) -> None:
        """Initialize empty coverage map."""
        self._coverage: dict[str, CoverageEntry] = {}

    def update_project(
        self,
        project: str,
        known: list[str] | None = None,
        unknown: list[str] | None = None,
        stale: list[str] | None = None,
    ) -> None:
        """Update coverage for a project.

        Args:
            project: Project name.
            known: List of known data categories.
            unknown: List of unknown data categories.
            stale: List of stale data categories with details.
        """
        entry = self._coverage.get(project, CoverageEntry(project=project))
        if known is not None:
            entry.known = known
        if unknown is not None:
            entry.unknown = unknown
        if stale is not None:
            entry.stale = stale
        self._coverage[project] = entry

    def get_coverage(self, project: str) -> CoverageEntry | None:
        """Get coverage entry for a project.

        Args:
            project: Project name.

        Returns:
            CoverageEntry or None.
        """
        return self._coverage.get(project)

    def generate_snapshot(self) -> dict[str, object]:
        """Generate coverage map snapshot data.

        Returns:
            Dict suitable for writing as JSON snapshot.
        """
        now = datetime.now(UTC).isoformat()
        projects: dict[str, dict[str, list[str]]] = {}
        for proj, entry in self._coverage.items():
            projects[proj] = {
                "known": entry.known,
                "unknown": entry.unknown,
                "stale": entry.stale,
            }
        return {
            "projects": projects,
            "generated_at": now,
        }

    def save_snapshot(self, snapshot_dir: Path) -> None:
        """Write coverage map to snapshot file.

        Args:
            snapshot_dir: Path to data/snapshots/ directory.
        """
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / "memory_coverage.json"
        data = self.generate_snapshot()
        path.write_text(json.dumps(data, indent=2))

    @property
    def projects(self) -> list[str]:
        """Return list of tracked projects."""
        return list(self._coverage.keys())


class StalenessTracker:
    """Track staleness of all memory sources."""

    # Staleness thresholds in hours
    FRESH_HOURS = 24
    AGING_HOURS = 168  # 7 days
    STALE_HOURS = 720  # 30 days

    def __init__(self) -> None:
        """Initialize empty staleness tracker."""
        self._entries: list[StalenessEntry] = []

    def add_source(
        self,
        name: str,
        source_type: str,
        last_updated: str,
    ) -> StalenessEntry:
        """Track a memory source.

        Args:
            name: Source name.
            source_type: Type (snapshot, database, file).
            last_updated: ISO 8601 timestamp of last update.

        Returns:
            StalenessEntry with computed status.
        """
        age_hours = self._compute_age_hours(last_updated)
        status = self._classify_staleness(age_hours)
        entry = StalenessEntry(
            name=name,
            source_type=source_type,
            age_hours=round(age_hours, 1),
            status=status,
            last_updated=last_updated,
        )
        # Replace existing entry for same name
        self._entries = [e for e in self._entries if e.name != name]
        self._entries.append(entry)
        return entry

    def get_stale_sources(self) -> list[StalenessEntry]:
        """Get all stale or critical sources.

        Returns:
            List of entries with stale or critical status.
        """
        return [e for e in self._entries if e.status in ("stale", "critical")]

    def generate_snapshot(self) -> dict[str, object]:
        """Generate staleness dashboard snapshot data.

        Returns:
            Dict suitable for writing as JSON snapshot.
        """
        now = datetime.now(UTC).isoformat()
        by_type: dict[str, dict[str, object]] = {}
        for entry in self._entries:
            by_type[entry.name] = {
                "source_type": entry.source_type,
                "age_hours": entry.age_hours,
                "status": entry.status,
                "last_updated": entry.last_updated,
            }
        return {
            "sources": by_type,
            "generated_at": now,
            "summary": {
                "total": len(self._entries),
                "fresh": sum(1 for e in self._entries if e.status == "fresh"),
                "aging": sum(1 for e in self._entries if e.status == "aging"),
                "stale": sum(1 for e in self._entries if e.status == "stale"),
                "critical": sum(
                    1 for e in self._entries if e.status == "critical"
                ),
            },
        }

    def save_snapshot(self, snapshot_dir: Path) -> None:
        """Write staleness dashboard to snapshot file.

        Args:
            snapshot_dir: Path to data/snapshots/ directory.
        """
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / "memory_staleness.json"
        data = self.generate_snapshot()
        path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _compute_age_hours(timestamp_str: str) -> float:
        """Compute age in hours from ISO 8601 timestamp."""
        try:
            ts = datetime.fromisoformat(timestamp_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            delta = now - ts
            return delta.total_seconds() / 3600
        except (ValueError, TypeError):
            return 999999.0  # Unknown = treat as critical

    @classmethod
    def _classify_staleness(cls, age_hours: float) -> str:
        """Classify staleness based on age in hours."""
        if age_hours < cls.FRESH_HOURS:
            return "fresh"
        if age_hours < cls.AGING_HOURS:
            return "aging"
        if age_hours < cls.STALE_HOURS:
            return "stale"
        return "critical"

    @property
    def entries(self) -> list[StalenessEntry]:
        """Return all tracked entries."""
        return list(self._entries)
