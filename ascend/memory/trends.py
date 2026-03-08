"""Historical trend aggregation from audit.jsonl.

Aggregates weekly stats: success rates, execution costs, response times.
Morning briefing shows trajectory, not just current status.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WeeklyAgentStats:
    """Aggregated stats for one agent over one week."""

    agent_type: str
    week_start: str
    total_runs: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    avg_duration_seconds: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0

    @property
    def success_rate(self) -> float:
        """Compute success rate as percentage."""
        if self.total_runs == 0:
            return 0.0
        return (self.successes / self.total_runs) * 100

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        d = asdict(self)
        d["success_rate"] = round(self.success_rate, 1)
        return d


@dataclass
class WeeklySummary:
    """Overall weekly summary across all agents."""

    week_start: str
    week_end: str
    total_runs: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    agent_stats: list[WeeklyAgentStats] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Initialize mutable default."""
        if self.agent_stats is None:
            self.agent_stats = []

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return {
            "week_start": self.week_start,
            "week_end": self.week_end,
            "total_runs": self.total_runs,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "success_rate": round(
                (self.total_successes / self.total_runs * 100)
                if self.total_runs > 0 else 0.0, 1,
            ),
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "agent_stats": [s.to_dict() for s in self.agent_stats],
        }


class TrendAggregator:
    """Aggregates audit.jsonl into weekly trend summaries.

    Stored in data/trends/ as weekly JSON files.
    """

    def __init__(
        self,
        audit_path: Path,
        trends_dir: Path,
    ) -> None:
        """Initialize trend aggregator.

        Args:
            audit_path: Path to logs/audit.jsonl.
            trends_dir: Directory for trend output (data/trends/).
        """
        self._audit_path = audit_path
        self._trends_dir = trends_dir

    def aggregate_week(
        self,
        week_start: datetime | None = None,
    ) -> WeeklySummary | None:
        """Aggregate audit events for a specific week.

        Args:
            week_start: Start of week (defaults to last Monday).

        Returns:
            WeeklySummary or None if no data.
        """
        if week_start is None:
            now = datetime.now(UTC)
            days_since_monday = now.weekday()
            week_start = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )

        week_end = week_start + timedelta(days=7)

        if not self._audit_path.exists():
            logger.warning("Audit log not found: %s", self._audit_path)
            return None

        # Parse relevant audit events
        agent_data: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {
                "durations": [],
                "tokens": [],
                "costs": [],
                "statuses": [],
            },
        )

        try:
            with open(self._audit_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = event.get("timestamp", "")
                    if not ts_str:
                        continue

                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue

                    if not (week_start <= ts < week_end):
                        continue

                    event_type = event.get("event", "")
                    if event_type != "task_completed":
                        continue

                    agent = event.get("agent_type", event.get("agent_id", "unknown"))
                    status = event.get("status", "")
                    duration = float(event.get("duration_seconds", 0))
                    tokens = int(event.get("tokens_used", 0))
                    cost = float(event.get("estimated_cost_usd", 0))

                    data = agent_data[agent]
                    data["durations"].append(duration)
                    data["tokens"].append(float(tokens))
                    data["costs"].append(cost)
                    data["statuses"].append(
                        1.0 if status == "success" else 0.0,
                    )
        except OSError as exc:
            logger.warning("Failed to read audit log: %s", exc)
            return None

        if not agent_data:
            return None

        # Build summary
        summary = WeeklySummary(
            week_start=week_start.isoformat(),
            week_end=week_end.isoformat(),
        )

        for agent, data in sorted(agent_data.items()):
            total = len(data["statuses"])
            successes = int(sum(data["statuses"]))
            durations = data["durations"]
            avg_dur = sum(durations) / len(durations) if durations else 0.0
            total_tokens = int(sum(data["tokens"]))
            total_cost = sum(data["costs"])

            stats = WeeklyAgentStats(
                agent_type=agent,
                week_start=week_start.isoformat(),
                total_runs=total,
                successes=successes,
                failures=total - successes,
                avg_duration_seconds=round(avg_dur, 1),
                total_tokens=total_tokens,
                total_cost_usd=round(total_cost, 4),
            )
            summary.agent_stats.append(stats)
            summary.total_runs += total
            summary.total_successes += successes
            summary.total_failures += total - successes
            summary.total_tokens += total_tokens
            summary.total_cost_usd += total_cost

        return summary

    def save_weekly(self, summary: WeeklySummary) -> Path:
        """Save weekly summary to data/trends/.

        Args:
            summary: Weekly summary to save.

        Returns:
            Path to saved file.
        """
        self._trends_dir.mkdir(parents=True, exist_ok=True)
        # Use week_start date as filename
        date_str = summary.week_start[:10]
        path = self._trends_dir / f"week_{date_str}.json"
        path.write_text(json.dumps(summary.to_dict(), indent=2))
        logger.info("Saved weekly trend: %s", path)
        return path

    def load_recent(self, weeks: int = 4) -> list[WeeklySummary]:
        """Load most recent weekly summaries.

        Args:
            weeks: Number of weeks to load.

        Returns:
            List of summaries, most recent first.
        """
        if not self._trends_dir.exists():
            return []

        files = sorted(
            self._trends_dir.glob("week_*.json"), reverse=True,
        )[:weeks]

        summaries: list[WeeklySummary] = []
        for path in files:
            try:
                data = json.loads(path.read_text())
                summary = WeeklySummary(
                    week_start=str(data.get("week_start", "")),
                    week_end=str(data.get("week_end", "")),
                    total_runs=int(data.get("total_runs", 0)),
                    total_successes=int(data.get("total_successes", 0)),
                    total_failures=int(data.get("total_failures", 0)),
                    total_tokens=int(data.get("total_tokens", 0)),
                    total_cost_usd=float(data.get("total_cost_usd", 0)),
                )
                summaries.append(summary)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load trend %s: %s", path, exc)

        return summaries

    def get_for_briefing(self, weeks: int = 4) -> str:
        """Get formatted trend summary for morning briefing.

        Args:
            weeks: Number of weeks to compare.

        Returns:
            Formatted trend text or empty string.
        """
        summaries = self.load_recent(weeks)
        if not summaries:
            return ""

        parts: list[str] = ["## Weekly Trends"]
        for s in summaries:
            rate = (
                round(s.total_successes / s.total_runs * 100, 1)
                if s.total_runs > 0 else 0.0
            )
            parts.append(
                f"Week {s.week_start[:10]}: "
                f"{s.total_runs} runs, {rate}% success, "
                f"${s.total_cost_usd:.2f} cost"
            )

        if len(summaries) >= 2:
            curr = summaries[0]
            prev = summaries[1]
            curr_rate = (
                curr.total_successes / curr.total_runs * 100
                if curr.total_runs > 0 else 0
            )
            prev_rate = (
                prev.total_successes / prev.total_runs * 100
                if prev.total_runs > 0 else 0
            )
            diff = curr_rate - prev_rate
            if diff > 2:
                parts.append("Trend: IMPROVING")
            elif diff < -2:
                parts.append("Trend: DECLINING")
            else:
                parts.append("Trend: STABLE")

        return "\n".join(parts)
