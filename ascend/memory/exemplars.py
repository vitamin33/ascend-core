"""Exemplar store for agent episodic memory.

Stores top-N successful outputs per agent as few-shot examples.
Research shows 3-5 golden outputs lift agent quality 20-30%.
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

_DEFAULT_MAX_EXEMPLARS = 5
_DEFAULT_INJECT_COUNT = 3


@dataclass
class Exemplar:
    """A high-quality agent output saved as a few-shot example."""

    task_description: str
    output: str
    quality_score: float
    human_feedback: str
    selected_at: str
    source_task_id: str
    tokens: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Exemplar:
        """Deserialize from dict."""
        return cls(
            task_description=str(data.get("task_description", "")),
            output=str(data.get("output", "")),
            quality_score=float(str(data.get("quality_score", 0.0))),
            human_feedback=str(data.get("human_feedback", "")),
            selected_at=str(data.get("selected_at", "")),
            source_task_id=str(data.get("source_task_id", "")),
            tokens=int(str(data.get("tokens", 0))),
        )


class ExemplarStore:
    """Manages per-agent exemplar storage.

    Keeps top N exemplars per agent, replacing lowest-scoring
    when a better one arrives.
    """

    def __init__(
        self,
        base_dir: Path,
        max_exemplars: int = _DEFAULT_MAX_EXEMPLARS,
    ) -> None:
        """Initialize exemplar store.

        Args:
            base_dir: Root directory for exemplar storage (data/exemplars/).
            max_exemplars: Maximum exemplars to keep per agent.
        """
        self._base_dir = base_dir
        self._max_exemplars = max_exemplars

    def _agent_dir(self, agent_type: str) -> Path:
        """Get directory for an agent's exemplars."""
        return self._base_dir / agent_type

    def _load_with_paths(
        self, agent_type: str,
    ) -> list[tuple[Path, Exemplar]]:
        """Load exemplars with their file paths.

        Returns:
            List of (path, exemplar) tuples.
        """
        agent_dir = self._agent_dir(agent_type)
        if not agent_dir.exists():
            return []

        results: list[tuple[Path, Exemplar]] = []
        for path in sorted(agent_dir.glob("exemplar_*.json")):
            try:
                data = json.loads(path.read_text())
                results.append((path, Exemplar.from_dict(data)))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load exemplar %s: %s", path, exc)
        return results

    def save(self, agent_type: str, exemplar: Exemplar) -> bool:
        """Save an exemplar, replacing the lowest-scoring if at capacity.

        Args:
            agent_type: Agent identifier.
            exemplar: Exemplar to save.

        Returns:
            True if saved (new or replaced lower), False if not good enough.
        """
        agent_dir = self._agent_dir(agent_type)
        agent_dir.mkdir(parents=True, exist_ok=True)

        existing = self._load_with_paths(agent_type)

        if len(existing) < self._max_exemplars:
            idx = len(existing) + 1
            path = agent_dir / f"exemplar_{idx:03d}.json"
            path.write_text(json.dumps(exemplar.to_dict(), indent=2))
            logger.info(
                "Saved exemplar %d for %s (score=%.1f)",
                idx, agent_type, exemplar.quality_score,
            )
            return True

        # At capacity — find lowest scoring by file path
        worst_path, worst_ex = existing[0]
        for path, ex in existing[1:]:
            if ex.quality_score < worst_ex.quality_score:
                worst_path = path
                worst_ex = ex

        if exemplar.quality_score > worst_ex.quality_score:
            worst_path.write_text(json.dumps(exemplar.to_dict(), indent=2))
            logger.info(
                "Replaced exemplar for %s (%.1f -> %.1f) at %s",
                agent_type, worst_ex.quality_score,
                exemplar.quality_score, worst_path.name,
            )
            return True

        logger.debug(
            "Exemplar not good enough for %s (%.1f <= %.1f)",
            agent_type, exemplar.quality_score, worst_ex.quality_score,
        )
        return False

    def load(self, agent_type: str) -> list[Exemplar]:
        """Load all exemplars for an agent, sorted by quality descending.

        Args:
            agent_type: Agent identifier.

        Returns:
            List of exemplars, highest quality first.
        """
        agent_dir = self._agent_dir(agent_type)
        if not agent_dir.exists():
            return []

        exemplars: list[Exemplar] = []
        for path in sorted(agent_dir.glob("exemplar_*.json")):
            try:
                data = json.loads(path.read_text())
                exemplars.append(Exemplar.from_dict(data))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load exemplar %s: %s", path, exc)

        exemplars.sort(key=lambda e: e.quality_score, reverse=True)
        return exemplars

    def get_for_injection(
        self,
        agent_type: str,
        count: int = _DEFAULT_INJECT_COUNT,
        max_tokens: int = 2000,
    ) -> str:
        """Get formatted exemplar context for injection into agent prompt.

        Args:
            agent_type: Agent identifier.
            count: Number of exemplars to inject.
            max_tokens: Approximate token budget for exemplars.

        Returns:
            Formatted exemplar section or empty string.
        """
        exemplars = self.load(agent_type)[:count]
        if not exemplars:
            return ""

        parts: list[str] = ["## High-Quality Output Examples"]
        total_tokens = 0
        for i, ex in enumerate(exemplars, 1):
            if total_tokens + ex.tokens > max_tokens:
                break
            entry = (
                f"\n### Example {i} (score: {ex.quality_score:.1f})\n"
                f"Task: {ex.task_description}\n"
                f"Output:\n{ex.output}"
            )
            if ex.human_feedback:
                entry += f"\nFeedback: {ex.human_feedback}"
            parts.append(entry)
            total_tokens += ex.tokens

        return "\n".join(parts)

    def create_exemplar(
        self,
        task_description: str,
        output: str,
        quality_score: float,
        task_id: str,
        human_feedback: str = "",
    ) -> Exemplar:
        """Create an Exemplar instance with current timestamp.

        Args:
            task_description: What the task was.
            output: The high-quality output.
            quality_score: Score from content_queue or manual.
            task_id: Source task ID.
            human_feedback: Optional human feedback text.

        Returns:
            New Exemplar instance.
        """
        # Rough token estimate: ~4 chars per token
        tokens = len(output) // 4
        return Exemplar(
            task_description=task_description,
            output=output,
            quality_score=quality_score,
            human_feedback=human_feedback,
            selected_at=datetime.now(UTC).isoformat(),
            source_task_id=task_id,
            tokens=tokens,
        )
