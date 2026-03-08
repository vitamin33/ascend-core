"""Failure and success pattern mining from audit.jsonl."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FailurePattern:
    """A grouped failure pattern from audit analysis."""

    error_type: str
    count: int
    last_seen: str
    agent_ids: list[str] = field(default_factory=list)
    example_task_id: str = ""


@dataclass
class SuccessPattern:
    """A grouped success pattern from audit analysis."""

    agent_id: str
    success_count: int
    avg_duration: float
    common_engine: str
    top_corrections: list[str] = field(default_factory=list)


class TraceAnalyzer:
    """Analyze audit.jsonl for failure patterns and agent statistics."""

    def __init__(self, audit_path: Path) -> None:
        """Initialize with path to audit JSONL file."""
        self._audit_path = audit_path

    def analyze_failures(self, hours: int = 24) -> list[FailurePattern]:
        """Read audit.jsonl, find failure/timeout/rejected events.

        Groups by error type, returns sorted by count descending.

        Args:
            hours: Only include events from the last N hours.

        Returns:
            List of FailurePattern sorted by count desc.
        """
        events = self._read_events(hours)
        groups: dict[str, list[dict[str, object]]] = defaultdict(list)

        for event in events:
            error_type = self._classify_event(event)
            if error_type:
                groups[error_type].append(event)

        patterns: list[FailurePattern] = []
        for error_type, group_events in groups.items():
            agent_ids = list({
                str(e.get("agent_id", "unknown"))
                for e in group_events
            })
            last_seen = str(group_events[-1].get("timestamp", ""))
            example_id = str(group_events[0].get("task_id", ""))

            patterns.append(FailurePattern(
                error_type=error_type,
                count=len(group_events),
                last_seen=last_seen,
                agent_ids=sorted(agent_ids),
                example_task_id=example_id,
            ))

        patterns.sort(key=lambda p: p.count, reverse=True)
        return patterns

    def get_agent_stats(
        self, hours: int = 24,
    ) -> dict[str, dict[str, int]]:
        """Per-agent success/failure/timeout/rejected counts.

        Args:
            hours: Only include events from the last N hours.

        Returns:
            Dict mapping agent_id to {status: count}.
        """
        events = self._read_events(hours)
        stats: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )

        for event in events:
            agent_id = str(event.get("agent_id", ""))
            if not agent_id:
                continue

            event_type = str(event.get("event", ""))
            if event_type == "task_completed":
                status = str(event.get("status", "unknown"))
                stats[agent_id][status] += 1
            elif event_type == "task_rejected":
                stats[agent_id]["rejected"] += 1

        # Convert nested defaultdicts to regular dicts
        return {
            agent: dict(counts)
            for agent, counts in stats.items()
        }

    def suggest_actions(
        self, patterns: list[FailurePattern],
    ) -> list[str]:
        """Generate rule-based suggestions from failure patterns.

        Args:
            patterns: List of FailurePattern from analyze_failures.

        Returns:
            List of actionable suggestion strings.
        """
        suggestions: list[str] = []

        for pattern in patterns:
            agents = ", ".join(pattern.agent_ids[:3])

            if pattern.error_type == "timeout" and pattern.count >= 3:
                suggestions.append(
                    f"Agent(s) {agents} had {pattern.count} timeouts "
                    f"— consider increasing timeout or checking "
                    f"loop detection config"
                )
            elif pattern.error_type == "timeout":
                suggestions.append(
                    f"Agent(s) {agents} had {pattern.count} timeout(s) "
                    f"— monitor for recurrence"
                )
            elif "rejected:policy" in pattern.error_type:
                suggestions.append(
                    f"Agent(s) {agents} had {pattern.count} policy "
                    f"rejection(s) — review policies.yaml allowed "
                    f"actions for these agents"
                )
            elif "rejected:trust" in pattern.error_type:
                suggestions.append(
                    f"Agent(s) {agents} had {pattern.count} trust "
                    f"rejection(s) — check if trust level promotion "
                    f"is needed"
                )
            elif pattern.error_type == "failure":
                suggestions.append(
                    f"Agent(s) {agents} had {pattern.count} execution "
                    f"failure(s) — check task '{pattern.example_task_id}' "
                    f"for root cause"
                )

        return suggestions

    def analyze_successes(self, hours: int = 168) -> list[SuccessPattern]:
        """Mine audit.jsonl for successful runs, grouped by agent.

        Args:
            hours: Time window (default 168 = 7 days).

        Returns:
            List of SuccessPattern sorted by success count desc.
        """
        events = self._read_events(hours)
        agent_successes: dict[str, list[dict[str, object]]] = defaultdict(list)

        for event in events:
            if (str(event.get("event")) == "task_completed"
                    and str(event.get("status")) == "success"):
                agent_id = str(event.get("agent_id", "unknown"))
                agent_successes[agent_id].append(event)

        corrections_dir = self._audit_path.parent.parent / "data" / "corrections"
        patterns: list[SuccessPattern] = []

        for agent_id, successes in agent_successes.items():
            durations = []
            engines: dict[str, int] = defaultdict(int)
            for ev in successes:
                dur = ev.get("duration_seconds")
                if dur is not None:
                    durations.append(float(str(dur)))
                engine = str(ev.get("engine", "unknown"))
                engines[engine] += 1

            avg_dur = sum(durations) / len(durations) if durations else 0.0
            common_engine = max(engines, key=engines.get) if engines else "unknown"  # type: ignore[arg-type]

            # Load corrections for this agent
            top_corrections: list[str] = []
            corr_file = corrections_dir / f"{agent_id}.log"
            if corr_file.exists():
                lines = corr_file.read_text().splitlines()
                top_corrections = [ln.strip() for ln in lines[-5:] if ln.strip()]

            patterns.append(SuccessPattern(
                agent_id=agent_id,
                success_count=len(successes),
                avg_duration=avg_dur,
                common_engine=common_engine,
                top_corrections=top_corrections,
            ))

        patterns.sort(key=lambda p: p.success_count, reverse=True)
        return patterns

    def suggest_prompt_improvements(self) -> list[str]:
        """Read correction logs and suggest cross-agent improvements.

        Returns:
            List of actionable prompt improvement suggestions.
        """
        corrections_dir = self._audit_path.parent.parent / "data" / "corrections"
        if not corrections_dir.exists():
            return []

        suggestions: list[str] = []
        agent_corrections: dict[str, list[str]] = {}

        for corr_file in corrections_dir.glob("*.log"):
            agent_id = corr_file.stem
            lines = [ln.strip() for ln in corr_file.read_text().splitlines() if ln.strip()]
            if lines:
                agent_corrections[agent_id] = lines

        # Find patterns across agents
        for agent_id, corrections in agent_corrections.items():
            recent = corrections[-3:]
            for correction in recent:
                lower = correction.lower()
                if "format" in lower or "structure" in lower:
                    suggestions.append(
                        f"{agent_id}: output format correction — "
                        f"review prompt structure requirements"
                    )
                elif "missing" in lower or "incomplete" in lower:
                    suggestions.append(
                        f"{agent_id}: missing data — "
                        f"add data source instructions to prompt"
                    )
                elif "tone" in lower or "voice" in lower:
                    suggestions.append(
                        f"{agent_id}: voice/tone correction — "
                        f"strengthen voice guidelines in prompt"
                    )

        return suggestions[:10]

    def analyze_time_patterns(
        self, hours: int = 168,
    ) -> dict[str, object]:
        """Group audit events by hour-of-day and day-of-week.

        Args:
            hours: Time window (default 168 = 7 days).

        Returns:
            Dict with best/worst hours and per-agent time patterns.
        """
        events = self._read_events(hours)
        hour_stats: dict[int, dict[str, int]] = defaultdict(
            lambda: {"success": 0, "failure": 0, "total": 0},
        )
        agent_hours: dict[str, dict[int, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"success": 0, "failure": 0}),
        )

        for event in events:
            if str(event.get("event")) != "task_completed":
                continue
            ts_str = str(event.get("timestamp", ""))
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                hour = ts.hour
            except ValueError:
                continue

            status = str(event.get("status", ""))
            agent_id = str(event.get("agent_id", ""))

            hour_stats[hour]["total"] += 1
            if status == "success":
                hour_stats[hour]["success"] += 1
                if agent_id:
                    agent_hours[agent_id][hour]["success"] += 1
            elif status in ("failure", "timeout"):
                hour_stats[hour]["failure"] += 1
                if agent_id:
                    agent_hours[agent_id][hour]["failure"] += 1

        # Find best/worst hours
        best_hour = -1
        best_rate = 0.0
        worst_hour = -1
        worst_rate = 1.0

        for hour, stats in hour_stats.items():
            total = stats["total"]
            if total < 3:
                continue
            rate = stats["success"] / total
            if rate > best_rate:
                best_rate = rate
                best_hour = hour
            if rate < worst_rate:
                worst_rate = rate
                worst_hour = hour

        return {
            "best_hour": {"hour": best_hour, "success_rate": round(best_rate, 2)},
            "worst_hour": {"hour": worst_hour, "success_rate": round(worst_rate, 2)},
            "hours_analyzed": len(hour_stats),
            "total_events": sum(s["total"] for s in hour_stats.values()),
        }

    def _read_events(self, hours: int) -> list[dict[str, object]]:
        """Read and filter audit events within time window."""
        if not self._audit_path.exists():
            return []

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        events: list[dict[str, object]] = []

        with open(self._audit_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed audit line")
                    continue

                ts_str = str(event.get("timestamp", ""))
                if not ts_str:
                    continue

                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts >= cutoff:
                        events.append(event)
                except ValueError:
                    continue

        return events

    def consume_eval_results(
        self, skill: str, hours: int = 168,
    ) -> list[dict[str, object]]:
        """Load eval suite results from data/evals/{skill}/iteration-N/results.json.

        Args:
            skill: Skill name (e.g. "morning-briefing").
            hours: Not used for file-based results — kept for API consistency.

        Returns:
            List of result dicts sorted by iteration descending (newest first).
        """
        evals_dir = self._audit_path.parent.parent / "data" / "evals" / skill
        if not evals_dir.exists():
            return []
        results: list[dict[str, object]] = []
        for iter_dir in sorted(evals_dir.glob("iteration-*"), reverse=True):
            results_file = iter_dir / "results.json"
            if not results_file.exists():
                continue
            try:
                data = json.loads(results_file.read_text())
                results.append(dict(data))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load eval results from %s: %s", results_file, exc)
        return results

    def trend_pass_rate(self, skill: str) -> dict[str, float]:
        """Return {iteration_label: pass_rate} for the given skill.

        Useful for charting progress in morning briefing or CLI output.

        Args:
            skill: Skill name.

        Returns:
            Dict mapping "iteration-N" → pass_rate (0.0–1.0), sorted ascending.
        """
        raw = self.consume_eval_results(skill)
        trend: dict[str, float] = {}
        for entry in reversed(raw):  # oldest first
            iteration = entry.get("iteration", 0)
            pass_rate = entry.get("pass_rate", 0.0)
            label = f"iteration-{iteration}"
            trend[label] = float(str(pass_rate))
        return trend

    def analyze_eval_quality(self) -> list[dict[str, object]]:
        """Flag flaky agents (high pass_rate variance across iterations).

        Requires 3+ iterations per skill. Returns list of
        {skill, issue, stddev, iterations} dicts.
        """
        import statistics

        evals_root = self._audit_path.parent.parent / "data" / "evals"
        if not evals_root.exists():
            return []
        flags: list[dict[str, object]] = []
        for skill_dir in evals_root.iterdir():
            if not skill_dir.is_dir():
                continue
            iterations = sorted(skill_dir.glob("iteration-*"))
            if len(iterations) < 3:
                continue
            rates: list[float] = []
            for it in iterations[-5:]:
                results_file = it / "results.json"
                if not results_file.exists():
                    continue
                try:
                    data = json.loads(results_file.read_text())
                    rates.append(float(str(data.get("pass_rate", 0))))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
            if len(rates) < 2:
                continue
            stddev = statistics.stdev(rates)
            if stddev > 0.2:
                flags.append({
                    "skill": skill_dir.name,
                    "issue": "high_variance",
                    "stddev": round(stddev, 4),
                    "iterations": len(rates),
                })
        return sorted(flags, key=lambda f: float(str(f["stddev"])), reverse=True)

    @staticmethod
    def _classify_event(event: dict[str, object]) -> str:
        """Classify an event into an error type string.

        Returns empty string for non-failure events.
        """
        event_type = str(event.get("event", ""))

        if event_type == "task_completed":
            status = str(event.get("status", ""))
            if status in ("failure", "timeout"):
                return status
        elif event_type == "task_rejected":
            reason = str(event.get("reason", "unknown"))
            return f"rejected:{reason}"

        return ""
