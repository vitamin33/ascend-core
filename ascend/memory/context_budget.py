"""Dynamic context budget allocation for agent runs.

Replaces flat 3000-char truncation with scored selection within a token budget.
Priority scoring: relevance(0.6) x recency(0.25) x importance(0.15).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ascend.memory.snapshot_meta import SnapshotMeta

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 5000
_RELEVANCE_WEIGHT = 0.6
_RECENCY_WEIGHT = 0.25
_IMPORTANCE_WEIGHT = 0.15


@dataclass
class ContextItem:
    """A scored context item ready for selection."""

    name: str
    content: str
    score: float
    tokens: int
    staleness_label: str = ""


class ContextBudget:
    """Dynamic context budget allocation per agent run.

    Scores context items by relevance, recency, and importance,
    then selects top-K items within the token budget.
    """

    def __init__(self, max_tokens: int = _DEFAULT_MAX_TOKENS) -> None:
        """Initialize budget.

        Args:
            max_tokens: Maximum total tokens for injected context.
        """
        self._max_tokens = max_tokens

    def allocate(
        self,
        agent_type: str,
        snapshot_names: list[str],
        snapshots: dict[str, tuple[str, SnapshotMeta | None]],
    ) -> list[ContextItem]:
        """Score and select context items within budget.

        Args:
            agent_type: The agent requesting context.
            snapshot_names: Snapshot names this agent wants (from AGENT_SNAPSHOT_MAP).
            snapshots: Map of name -> (content, metadata).

        Returns:
            Selected context items, highest-scoring first.
        """
        scored: list[ContextItem] = []

        for name in snapshot_names:
            if name not in snapshots:
                continue
            content, meta = snapshots[name]
            if not content:
                continue

            # Skip snapshots older than 7 days from budget scoring
            staleness = meta.staleness_label() if meta else "unknown"
            if staleness in ("stale", "critical"):
                logger.debug("Skipping %s snapshot: %s", staleness, name)
                continue

            tokens = meta.token_count if meta else len(content) // 4
            score = self._score(name, agent_type, meta)

            scored.append(ContextItem(
                name=name,
                content=content,
                score=score,
                tokens=tokens,
                staleness_label=staleness,
            ))

        scored.sort(key=lambda x: x.score, reverse=True)

        selected: list[ContextItem] = []
        used = 0
        for item in scored:
            if used + item.tokens <= self._max_tokens:
                selected.append(item)
                used += item.tokens

        if scored and not selected:
            # Always include at least one item, even if over budget
            selected = [scored[0]]

        logger.debug(
            "Context budget for %s: %d/%d tokens, %d/%d items",
            agent_type, used, self._max_tokens,
            len(selected), len(scored),
        )
        return selected

    def _score(
        self,
        name: str,
        agent_type: str,
        meta: SnapshotMeta | None,
    ) -> float:
        """Compute priority score for a context item.

        Returns:
            Float score between 0.0 and 1.0.
        """
        relevance = self._relevance_score(name, agent_type)
        recency = self._recency_score(meta)
        importance = self._importance_score(name)

        return (
            _RELEVANCE_WEIGHT * relevance
            + _RECENCY_WEIGHT * recency
            + _IMPORTANCE_WEIGHT * importance
        )

    @staticmethod
    def _relevance_score(name: str, agent_type: str) -> float:
        """Score how relevant a snapshot is to this agent type.

        Higher if the snapshot directly relates to the agent's domain.
        """
        # Direct self-reference = highest relevance
        if name == agent_type:
            return 1.0
        # Cross-project status is broadly relevant
        if name == "cross_project_status":
            return 0.8
        # Health data is relevant for monitoring agents
        if name == "health_monitor" and agent_type in (
            "morning_briefing", "dev_planner", "incident_triage",
        ):
            return 0.9
        # Content data for content agents
        if name in ("content_queue", "content_performance") and agent_type in (
            "seo_writer", "content_pipeline", "newsletter", "case_study_writer",
        ):
            return 0.9
        # Default moderate relevance (agent snapshot map already filters)
        return 0.6

    @staticmethod
    def _recency_score(meta: SnapshotMeta | None) -> float:
        """Score based on how recent the data is.

        Exponential decay: 1.0 at 0h, 0.5 at 24h, ~0.25 at 48h.
        """
        if not meta:
            return 0.3  # Unknown recency = low score

        try:
            ts = datetime.fromisoformat(meta.generated_at)
            hours = (datetime.now(UTC) - ts).total_seconds() / 3600
            # Exponential decay with half-life of 24 hours
            import math
            return max(0.1, math.exp(-0.693 * hours / 24))
        except (ValueError, TypeError):
            return 0.3

    @staticmethod
    def _importance_score(name: str) -> float:
        """Score base importance of a snapshot type.

        Some data sources are inherently more valuable.
        """
        high_importance = {
            "cross_project_status", "health_monitor", "time_tracker",
            "content_queue", "financial_summary",
        }
        medium_importance = {
            "tech_radar", "test_results", "social_proof",
            "content_performance", "prompt_variants",
        }
        if name in high_importance:
            return 1.0
        if name in medium_importance:
            return 0.7
        return 0.5
