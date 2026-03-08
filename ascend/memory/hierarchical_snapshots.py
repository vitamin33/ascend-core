"""Hierarchical snapshots — group and summarize snapshots for scale.

When snapshot count exceeds a threshold, snapshots are grouped by category
and a summary layer is generated. Agents receive category summaries instead
of all individual snapshots, reducing context size.
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

_MAX_INDIVIDUAL_SNAPSHOTS = 15
_SNAPSHOT_CATEGORIES: dict[str, list[str]] = {
    "health": [
        "health_monitor", "cross_project_status", "crest_health",
    ],
    "content": [
        "content_queue", "content_performance", "tech_radar",
        "social_proof",
    ],
    "financial": [
        "financial_summary", "time_tracker", "cost_analyzer",
    ],
    "development": [
        "test_results", "project_scan", "implementation_tracker",
    ],
    "research": [
        "nightly_specs", "nightly_research", "proactive_insights",
    ],
    "metrics": [
        "flow_kpi", "startup_metrics", "prompt_variants",
    ],
}


@dataclass
class SnapshotGroup:
    """A group of related snapshots with a summary."""

    category: str
    snapshots: list[str] = field(default_factory=list)
    summary: str = ""
    total_size_bytes: int = 0
    freshest_age_hours: float = 0.0
    stalest_age_hours: float = 0.0


class HierarchicalSnapshotManager:
    """Manage snapshot hierarchy for large snapshot sets."""

    def __init__(self, snapshot_dir: Path) -> None:
        """Initialize with snapshot directory.

        Args:
            snapshot_dir: Path to data/snapshots/.
        """
        self._dir = snapshot_dir

    def should_use_hierarchy(self) -> bool:
        """Check if snapshot count warrants hierarchical mode.

        Returns:
            True if more than _MAX_INDIVIDUAL_SNAPSHOTS exist.
        """
        if not self._dir.exists():
            return False
        count = sum(1 for _ in self._dir.glob("*.json"))
        return count > _MAX_INDIVIDUAL_SNAPSHOTS

    def categorize(self) -> dict[str, SnapshotGroup]:
        """Group all snapshots by category.

        Returns:
            Dict mapping category name to SnapshotGroup.
        """
        groups: dict[str, SnapshotGroup] = {}

        if not self._dir.exists():
            return groups

        # Build reverse lookup: snapshot_name -> category
        snap_to_cat: dict[str, str] = {}
        for cat, names in _SNAPSHOT_CATEGORIES.items():
            for name in names:
                snap_to_cat[name] = cat

        now = datetime.now(UTC)

        for path in sorted(self._dir.glob("*.json")):
            name = path.stem
            if name.startswith("_") or name.startswith("memory_"):
                continue  # Skip metadata files

            category = snap_to_cat.get(name, "other")
            if category not in groups:
                groups[category] = SnapshotGroup(category=category)

            group = groups[category]
            group.snapshots.append(name)

            try:
                stat = path.stat()
                group.total_size_bytes += stat.st_size
                mod_time = datetime.fromtimestamp(
                    stat.st_mtime, tz=UTC,
                )
                age_hours = (now - mod_time).total_seconds() / 3600

                if group.freshest_age_hours == 0 or age_hours < group.freshest_age_hours:
                    group.freshest_age_hours = round(age_hours, 1)
                if age_hours > group.stalest_age_hours:
                    group.stalest_age_hours = round(age_hours, 1)
            except OSError:
                continue

        return groups

    def generate_summary_layer(self) -> dict[str, object]:
        """Generate summary of all snapshot groups.

        Returns:
            Dict suitable for snapshot injection as overview.
        """
        groups = self.categorize()
        summary: dict[str, object] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "total_snapshots": sum(len(g.snapshots) for g in groups.values()),
            "categories": {},
        }

        for cat, group in groups.items():
            cat_data: dict[str, object] = {
                "snapshot_count": len(group.snapshots),
                "snapshots": group.snapshots,
                "total_size_kb": round(group.total_size_bytes / 1024, 1),
                "freshest_hours": group.freshest_age_hours,
                "stalest_hours": group.stalest_age_hours,
            }
            categories = summary["categories"]
            if isinstance(categories, dict):
                categories[cat] = cat_data

        return summary

    def get_category_snapshots(self, category: str) -> list[str]:
        """Get snapshot names for a specific category.

        Args:
            category: Category name (health, content, financial, etc).

        Returns:
            List of snapshot names in that category.
        """
        return _SNAPSHOT_CATEGORIES.get(category, [])

    def save_summary(self) -> None:
        """Write summary layer to snapshot directory."""
        summary = self.generate_summary_layer()
        path = self._dir / "_hierarchy.json"
        path.write_text(json.dumps(summary, indent=2))

    def get_for_injection(self, agent_categories: list[str]) -> str:
        """Get hierarchical context for specific categories.

        Args:
            agent_categories: Categories relevant to the agent.

        Returns:
            Formatted summary showing available data per category.
        """
        groups = self.categorize()
        if not groups:
            return ""

        parts: list[str] = ["## Available Data (Hierarchical View)"]
        for cat in agent_categories:
            group = groups.get(cat)
            if group:
                freshness = "fresh" if group.freshest_age_hours < 24 else "stale"
                parts.append(
                    f"- **{cat}**: {len(group.snapshots)} snapshots "
                    f"({freshness}, {round(group.total_size_bytes/1024)}KB)"
                )
            else:
                parts.append(f"- **{cat}**: no data available")

        return "\n".join(parts)
