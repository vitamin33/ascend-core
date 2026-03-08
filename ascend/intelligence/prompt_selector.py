"""Thompson Sampling prompt variant selector for A/B testing.

Manages prompt variants per agent with Beta(successes+1, failures+1)
sampling. Records outcomes from content_queue approve/reject signals
and auto-retires losing variants.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "trust.db"


@dataclass
class VariantStats:
    """Performance stats for a prompt variant."""

    agent_id: str
    variant_id: str
    prompt_suffix: str
    successes: int
    failures: int
    active: bool
    created_at: str

    @property
    def total_trials(self) -> int:
        """Total number of trials."""
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage."""
        if self.total_trials == 0:
            return 0.0
        return round(self.successes / self.total_trials * 100, 1)


class PromptSelector:
    """Thompson Sampling variant selector for prompt A/B testing."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize with SQLite database path.

        Args:
            db_path: Path to database. Defaults to trust.db.
        """
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create prompt_variants table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_variants (
                agent_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                prompt_suffix TEXT NOT NULL,
                successes INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY (agent_id, variant_id)
            )
        """)
        self._conn.commit()

    def add_variant(
        self,
        agent_id: str,
        variant_id: str,
        prompt_suffix: str,
    ) -> None:
        """Register a new prompt variant for an agent.

        Args:
            agent_id: Agent identifier.
            variant_id: Unique variant label (e.g. "A", "B").
            prompt_suffix: Text appended to the agent's prompt.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """INSERT OR IGNORE INTO prompt_variants
               (agent_id, variant_id, prompt_suffix, successes, failures, active, created_at)
               VALUES (?, ?, ?, 0, 0, 1, ?)""",
            (agent_id, variant_id, prompt_suffix, now),
        )
        self._conn.commit()

    def select(self, agent_id: str) -> tuple[str, str]:
        """Thompson Sample: pick the variant with highest Beta sample.

        Args:
            agent_id: Agent identifier.

        Returns:
            Tuple of (variant_id, prompt_suffix). Returns ("", "") if
            no active variants exist.
        """
        rows = self._conn.execute(
            """SELECT variant_id, prompt_suffix, successes, failures
               FROM prompt_variants
               WHERE agent_id = ? AND active = 1""",
            (agent_id,),
        ).fetchall()

        if not rows:
            return ("", "")

        best_score = -1.0
        best_variant = ""
        best_suffix = ""

        for row in rows:
            alpha = int(row["successes"]) + 1
            beta = int(row["failures"]) + 1
            sample = random.betavariate(alpha, beta)
            if sample > best_score:
                best_score = sample
                best_variant = str(row["variant_id"])
                best_suffix = str(row["prompt_suffix"])

        return (best_variant, best_suffix)

    def record_outcome(
        self,
        agent_id: str,
        variant_id: str,
        success: bool,
    ) -> None:
        """Record an outcome for a variant.

        Args:
            agent_id: Agent identifier.
            variant_id: Variant that was used.
            success: Whether the outcome was successful.
        """
        column = "successes" if success else "failures"
        self._conn.execute(
            f"""UPDATE prompt_variants
                SET {column} = {column} + 1
                WHERE agent_id = ? AND variant_id = ?""",
            (agent_id, variant_id),
        )
        self._conn.commit()

    def retire_losers(
        self,
        min_trials: int = 10,
        min_rate: float = 0.3,
    ) -> list[tuple[str, str]]:
        """Deactivate variants with low success rates after enough trials.

        Args:
            min_trials: Minimum trials before evaluating.
            min_rate: Minimum success rate to stay active.

        Returns:
            List of (agent_id, variant_id) pairs that were retired.
        """
        rows = self._conn.execute(
            """SELECT agent_id, variant_id, successes, failures
               FROM prompt_variants
               WHERE active = 1""",
        ).fetchall()

        retired: list[tuple[str, str]] = []
        for row in rows:
            total = int(row["successes"]) + int(row["failures"])
            if total < min_trials:
                continue
            rate = int(row["successes"]) / total
            if rate < min_rate:
                agent = str(row["agent_id"])
                variant = str(row["variant_id"])
                # Only retire if agent has other active variants
                active_count = self._conn.execute(
                    """SELECT COUNT(*) FROM prompt_variants
                       WHERE agent_id = ? AND active = 1""",
                    (agent,),
                ).fetchone()[0]
                if active_count > 1:
                    self._conn.execute(
                        """UPDATE prompt_variants
                           SET active = 0
                           WHERE agent_id = ? AND variant_id = ?""",
                        (agent, variant),
                    )
                    retired.append((agent, variant))

        if retired:
            self._conn.commit()
            logger.info("Retired %d losing variants", len(retired))

        return retired

    def get_stats(self, agent_id: str) -> list[VariantStats]:
        """Get performance stats for all variants of an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            List of VariantStats sorted by success rate descending.
        """
        rows = self._conn.execute(
            """SELECT agent_id, variant_id, prompt_suffix,
                      successes, failures, active, created_at
               FROM prompt_variants
               WHERE agent_id = ?
               ORDER BY
                   CASE WHEN (successes + failures) > 0
                        THEN CAST(successes AS REAL) / (successes + failures)
                        ELSE 0 END DESC""",
            (agent_id,),
        ).fetchall()

        return [
            VariantStats(
                agent_id=str(row["agent_id"]),
                variant_id=str(row["variant_id"]),
                prompt_suffix=str(row["prompt_suffix"]),
                successes=int(row["successes"]),
                failures=int(row["failures"]),
                active=bool(row["active"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_all_active_stats(self) -> dict[str, list[VariantStats]]:
        """Get stats for all agents with active A/B tests.

        Returns:
            Dict mapping agent_id to list of VariantStats.
        """
        rows = self._conn.execute(
            """SELECT DISTINCT agent_id FROM prompt_variants
               WHERE active = 1""",
        ).fetchall()

        results: dict[str, list[VariantStats]] = {}
        for row in rows:
            agent_id = str(row["agent_id"])
            stats = self.get_stats(agent_id)
            if len(stats) > 1:  # Only include if agent has 2+ variants
                results[agent_id] = stats

        return results

    def format_briefing_section(self) -> str:
        """Format variant stats for morning briefing inclusion.

        Returns:
            Formatted string or empty if no active tests.
        """
        all_stats = self.get_all_active_stats()
        if not all_stats:
            return ""

        lines = ["## Prompt A/B Tests"]
        for agent_id, variants in all_stats.items():
            active = [v for v in variants if v.active]
            if len(active) < 2:
                continue
            parts: list[str] = []
            for v in active:
                parts.append(f"{v.variant_id} ({v.success_rate}%)")
            remaining = max(0, 10 - min(v.total_trials for v in active))
            line = f"- {agent_id}: {' vs '.join(parts)}"
            if remaining > 0:
                line += f", {remaining} trials left"
            lines.append(line)

        return "\n".join(lines) if len(lines) > 1 else ""

    def write_snapshot(self, snapshot_path: Path | None = None) -> None:
        """Write variant stats to snapshot file for cross-agent awareness.

        Args:
            snapshot_path: Path to write. Defaults to data/snapshots/prompt_variants.json.
        """
        import json

        path = snapshot_path or (
            self._db_path.parent / "data" / "snapshots" / "prompt_variants.json"
        )
        all_stats = self.get_all_active_stats()
        if not all_stats:
            return

        data: dict[str, object] = {}
        for agent_id, variants in all_stats.items():
            data[agent_id] = [
                {
                    "variant_id": v.variant_id,
                    "success_rate": v.success_rate,
                    "total_trials": v.total_trials,
                    "active": v.active,
                }
                for v in variants
            ]

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
