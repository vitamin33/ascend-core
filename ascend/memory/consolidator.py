"""Consolidation pipeline — compress episodic memory into semantic summaries.

Over time, individual failure cases and exemplars accumulate. This module
compresses old episodes into aggregate summaries per agent, keeping the
most recent N episodes in detail and summarizing the rest.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_KEEP_RECENT = 10
_DEFAULT_MIN_EPISODES = 20


@dataclass
class ConsolidatedSummary:
    """Compressed summary of episodic memory for an agent."""

    agent_id: str
    total_episodes: int = 0
    consolidated_episodes: int = 0
    kept_recent: int = 0
    common_errors: list[str] = field(default_factory=list)
    common_patterns: list[str] = field(default_factory=list)
    success_rate: float = 0.0
    last_consolidated: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "total_episodes": self.total_episodes,
            "consolidated_episodes": self.consolidated_episodes,
            "kept_recent": self.kept_recent,
            "common_errors": self.common_errors,
            "common_patterns": self.common_patterns,
            "success_rate": round(self.success_rate, 2),
            "last_consolidated": self.last_consolidated,
        }


class ConsolidationPipeline:
    """Compress old episodic memory into semantic summaries."""

    def __init__(
        self,
        data_dir: Path,
        keep_recent: int = _DEFAULT_KEEP_RECENT,
        min_episodes: int = _DEFAULT_MIN_EPISODES,
    ) -> None:
        """Initialize consolidation pipeline.

        Args:
            data_dir: Root data directory (contains episodes/, exemplars/).
            keep_recent: Number of recent episodes to keep in full detail.
            min_episodes: Minimum episodes before consolidation triggers.
        """
        self._data_dir = data_dir
        self._keep_recent = keep_recent
        self._min_episodes = min_episodes
        self._summaries_dir = data_dir / "consolidated"
        self._summaries_dir.mkdir(parents=True, exist_ok=True)

    def consolidate_failures(self, agent_id: str) -> ConsolidatedSummary | None:
        """Consolidate failure episodes for an agent.

        Args:
            agent_id: The agent to consolidate.

        Returns:
            ConsolidatedSummary or None if too few episodes.
        """
        failures_dir = self._data_dir / "episodes" / "failures" / agent_id
        if not failures_dir.exists():
            return None

        files = sorted(failures_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if len(files) < self._min_episodes:
            return None

        # Split into old (to consolidate) and recent (to keep)
        old_files = files[:-self._keep_recent]
        recent_files = files[-self._keep_recent:]

        # Extract patterns from old episodes
        error_counts: dict[str, int] = defaultdict(int)
        total = len(files)
        for path in old_files:
            try:
                data = json.loads(path.read_text())
                error = str(data.get("error", "unknown"))
                # Normalize error: take first 80 chars
                normalized = error[:80].strip()
                if normalized:
                    error_counts[normalized] += 1
            except (json.JSONDecodeError, OSError):
                continue

        # Top 5 most common errors
        common = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        common_errors = [f"{err} ({cnt}x)" for err, cnt in common]

        summary = ConsolidatedSummary(
            agent_id=agent_id,
            total_episodes=total,
            consolidated_episodes=len(old_files),
            kept_recent=len(recent_files),
            common_errors=common_errors,
            success_rate=0.0,  # All failures
            last_consolidated=datetime.now(UTC).isoformat(),
        )

        # Save summary
        self._save_summary(agent_id, "failures", summary)

        # Remove consolidated files
        for path in old_files:
            try:
                path.unlink()
            except OSError:
                continue

        logger.info(
            "Consolidated %d failure episodes for %s (kept %d recent)",
            len(old_files), agent_id, len(recent_files),
        )
        return summary

    def consolidate_exemplars(self, agent_id: str) -> ConsolidatedSummary | None:
        """Consolidate exemplar episodes for an agent.

        Args:
            agent_id: The agent to consolidate.

        Returns:
            ConsolidatedSummary or None if too few episodes.
        """
        exemplars_dir = self._data_dir / "exemplars" / agent_id
        if not exemplars_dir.exists():
            return None

        files = sorted(exemplars_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if len(files) < self._min_episodes:
            return None

        old_files = files[:-self._keep_recent]
        recent_files = files[-self._keep_recent:]

        # Extract patterns from old exemplars
        pattern_counts: dict[str, int] = defaultdict(int)
        quality_scores: list[float] = []
        for path in old_files:
            try:
                data = json.loads(path.read_text())
                score = float(data.get("quality_score", 0))
                quality_scores.append(score)
                tags = data.get("tags", [])
                if isinstance(tags, list):
                    for tag in tags:
                        pattern_counts[str(tag)] += 1
            except (json.JSONDecodeError, OSError):
                continue

        common = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        common_patterns = [f"{pat} ({cnt}x)" for pat, cnt in common]
        avg_quality = (
            sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        )

        summary = ConsolidatedSummary(
            agent_id=agent_id,
            total_episodes=len(files),
            consolidated_episodes=len(old_files),
            kept_recent=len(recent_files),
            common_patterns=common_patterns,
            success_rate=avg_quality,
            last_consolidated=datetime.now(UTC).isoformat(),
        )

        self._save_summary(agent_id, "exemplars", summary)

        for path in old_files:
            try:
                path.unlink()
            except OSError:
                continue

        logger.info(
            "Consolidated %d exemplar episodes for %s (kept %d recent)",
            len(old_files), agent_id, len(recent_files),
        )
        return summary

    def consolidate_all(self) -> dict[str, list[ConsolidatedSummary]]:
        """Run consolidation for all agents with enough episodes.

        Returns:
            Dict mapping category to list of summaries generated.
        """
        results: dict[str, list[ConsolidatedSummary]] = {
            "failures": [],
            "exemplars": [],
        }

        # Consolidate failures
        failures_root = self._data_dir / "episodes" / "failures"
        if failures_root.exists():
            for agent_dir in failures_root.iterdir():
                if agent_dir.is_dir():
                    summary = self.consolidate_failures(agent_dir.name)
                    if summary:
                        results["failures"].append(summary)

        # Consolidate exemplars
        exemplars_root = self._data_dir / "exemplars"
        if exemplars_root.exists():
            for agent_dir in exemplars_root.iterdir():
                if agent_dir.is_dir():
                    summary = self.consolidate_exemplars(agent_dir.name)
                    if summary:
                        results["exemplars"].append(summary)

        return results

    def get_summary(self, agent_id: str, category: str) -> ConsolidatedSummary | None:
        """Load a consolidated summary.

        Args:
            agent_id: The agent.
            category: "failures" or "exemplars".

        Returns:
            Summary or None.
        """
        path = self._summaries_dir / f"{agent_id}_{category}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return ConsolidatedSummary(
                agent_id=str(data.get("agent_id", agent_id)),
                total_episodes=int(data.get("total_episodes", 0)),
                consolidated_episodes=int(data.get("consolidated_episodes", 0)),
                kept_recent=int(data.get("kept_recent", 0)),
                common_errors=list(data.get("common_errors", [])),
                common_patterns=list(data.get("common_patterns", [])),
                success_rate=float(data.get("success_rate", 0.0)),
                last_consolidated=str(data.get("last_consolidated", "")),
            )
        except (json.JSONDecodeError, OSError):
            return None

    def get_for_injection(self, agent_id: str) -> str:
        """Get consolidated summaries for context injection.

        Args:
            agent_id: The agent.

        Returns:
            Formatted summary text or empty string.
        """
        parts: list[str] = []

        fail_summary = self.get_summary(agent_id, "failures")
        if fail_summary and fail_summary.common_errors:
            parts.append("## Historical Failure Patterns")
            for err in fail_summary.common_errors:
                parts.append(f"- {err}")

        ex_summary = self.get_summary(agent_id, "exemplars")
        if ex_summary and ex_summary.common_patterns:
            parts.append("## Historical Quality Patterns")
            for pat in ex_summary.common_patterns:
                parts.append(f"- {pat}")

        return "\n".join(parts)

    def _save_summary(
        self,
        agent_id: str,
        category: str,
        summary: ConsolidatedSummary,
    ) -> None:
        """Persist summary to disk."""
        path = self._summaries_dir / f"{agent_id}_{category}.json"
        path.write_text(json.dumps(summary.to_dict(), indent=2))
