"""Recovery engine — auto-detect failure patterns and take corrective action."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ascend.trust_engine import TrustEngine, TrustLevel

if TYPE_CHECKING:
    from pathlib import Path

    from ascend.intelligence.trace_analyzer import TraceAnalyzer

logger = logging.getLogger(__name__)

_TIMEOUT_THRESHOLD = 3
_REJECTION_THRESHOLD = 5
_BUDGET_WARNING_PCT = 80
_PLAYBOOK_MIN_OUTCOMES = 5


def _utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RecoveryAction:
    """A single recovery action recommended or executed by the engine."""

    action_type: str
    agent_id: str
    reason: str
    timestamp: str = field(default_factory=_utc_now)


@dataclass
class RecoveryOutcome:
    """Outcome of a recovery action for playbook learning."""

    failure_type: str
    agent_id: str
    recovery_attempted: str
    outcome: str  # success, failure, partial
    time_to_resolve: str
    recorded_at: str = field(default_factory=_utc_now)


@dataclass
class RecoveryPlaybook:
    """Auto-generated playbook from recovery outcome history."""

    failure_type: str
    best_action: str
    success_rate: float
    total_outcomes: int
    recommendation: str


class RecoveryEngine:
    """Analyzes failures and applies corrective actions."""

    def __init__(
        self,
        trust_engine: TrustEngine,
        trace_analyzer: TraceAnalyzer,
        audit_path: Path | None = None,
    ) -> None:
        """Initialize with trust engine, trace analyzer, and optional audit path.

        Args:
            trust_engine: TrustEngine for demotion actions.
            trace_analyzer: TraceAnalyzer for failure pattern analysis.
            audit_path: Path to audit.jsonl for persisting recovery actions.
        """
        self._trust = trust_engine
        self._analyzer = trace_analyzer
        self._audit_path = audit_path

    def evaluate(self, hours: int = 24) -> list[RecoveryAction]:
        """Analyze recent failures and return recommended recovery actions.

        Rules:
        - 3+ timeouts in 24h for same agent -> demote + pause (set to L0)
        - 5+ policy rejections for same agent -> flag for review
        - Budget at 80%+ -> log warning action

        Args:
            hours: Time window to analyze.

        Returns:
            List of recommended RecoveryAction items.
        """
        actions: list[RecoveryAction] = []
        stats = self._analyzer.get_agent_stats(hours=hours)

        for agent_id, counts in stats.items():
            timeout_count = counts.get("timeout", 0)
            if timeout_count >= _TIMEOUT_THRESHOLD:
                actions.append(RecoveryAction(
                    action_type="demote_and_pause",
                    agent_id=agent_id,
                    reason=(
                        f"{timeout_count} timeouts in {hours}h "
                        f"— demote to L0"
                    ),
                ))

            rejected_count = counts.get("rejected", 0)
            if rejected_count >= _REJECTION_THRESHOLD:
                actions.append(RecoveryAction(
                    action_type="flag_for_review",
                    agent_id=agent_id,
                    reason=(
                        f"{rejected_count} policy rejections in {hours}h "
                        f"— flag for human review"
                    ),
                ))

        return actions

    def evaluate_budget(
        self,
        daily_used: int,
        daily_limit: int,
    ) -> list[RecoveryAction]:
        """Check budget usage and return warning if threshold exceeded.

        Args:
            daily_used: Current daily API call count.
            daily_limit: Maximum daily API calls allowed.

        Returns:
            List with a budget warning action if over threshold, else empty.
        """
        if daily_limit <= 0:
            return []

        pct = (daily_used / daily_limit) * 100
        if pct >= _BUDGET_WARNING_PCT:
            return [RecoveryAction(
                action_type="budget_warning",
                agent_id="system",
                reason=(
                    f"Budget at {pct:.0f}% "
                    f"({daily_used}/{daily_limit} daily calls)"
                ),
            )]
        return []

    def execute_actions(
        self,
        actions: list[RecoveryAction],
    ) -> list[RecoveryAction]:
        """Apply recovery actions and return the list of executed actions.

        Args:
            actions: List of RecoveryAction items to execute.

        Returns:
            Same list (all are executed and logged to history).
        """
        for action in actions:
            if action.action_type == "demote_and_pause":
                self._apply_demotion(action.agent_id)
            elif action.action_type == "flag_for_review":
                logger.warning(
                    "REVIEW NEEDED: agent %s — %s",
                    action.agent_id, action.reason,
                )
            elif action.action_type == "budget_warning":
                logger.warning("BUDGET WARNING: %s", action.reason)

            self._log_action_to_audit(action)

        return actions

    def get_recent_actions(self, limit: int = 20) -> list[dict[str, str]]:
        """Return recent recovery actions from audit.jsonl.

        Args:
            limit: Maximum number of actions to return.

        Returns:
            List of dicts with action_type, agent_id, reason, timestamp.
        """
        actions = self._read_actions_from_audit(limit)
        return [
            {
                "action_type": str(a.get("action_type", "")),
                "agent_id": str(a.get("agent_id", "")),
                "reason": str(a.get("reason", "")),
                "timestamp": str(a.get("timestamp", "")),
            }
            for a in actions
        ]

    def _log_action_to_audit(self, action: RecoveryAction) -> None:
        """Append a recovery action event to audit.jsonl."""
        if self._audit_path is None:
            return
        entry = {
            "event": "recovery_action",
            "action_type": action.action_type,
            "agent_id": action.agent_id,
            "reason": action.reason,
            "timestamp": action.timestamp,
        }
        try:
            with open(self._audit_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.warning("Failed to write recovery action to audit")

    def _read_actions_from_audit(
        self, limit: int,
    ) -> list[dict[str, str]]:
        """Read recovery actions from audit.jsonl, newest first."""
        if self._audit_path is None or not self._audit_path.exists():
            return []
        actions: list[dict[str, str]] = []
        try:
            with open(self._audit_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "recovery_action":
                        actions.append(event)
        except OSError:
            logger.warning("Failed to read recovery actions from audit")
        return list(reversed(actions[-limit:]))

    def record_outcome(self, outcome: RecoveryOutcome) -> None:
        """Record a recovery action outcome for playbook learning.

        Args:
            outcome: The recovery outcome to record.
        """
        if self._audit_path is None:
            return
        entry = {
            "event": "recovery_outcome",
            "failure_type": outcome.failure_type,
            "agent_id": outcome.agent_id,
            "recovery_attempted": outcome.recovery_attempted,
            "outcome": outcome.outcome,
            "time_to_resolve": outcome.time_to_resolve,
            "recorded_at": outcome.recorded_at,
        }
        try:
            with open(self._audit_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.warning("Failed to write recovery outcome to audit")

    def generate_playbooks(self) -> list[RecoveryPlaybook]:
        """Auto-generate playbooks from recovery outcome history.

        Requires 5+ outcomes per failure type to generate a playbook.

        Returns:
            List of generated playbooks.
        """
        outcomes = self._read_outcomes()
        if not outcomes:
            return []

        # Group by failure_type
        by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
        for o in outcomes:
            by_type[o.get("failure_type", "unknown")].append(o)

        playbooks: list[RecoveryPlaybook] = []
        for failure_type, type_outcomes in by_type.items():
            if len(type_outcomes) < _PLAYBOOK_MIN_OUTCOMES:
                continue

            # Count success rate per recovery action
            action_stats: dict[str, tuple[int, int]] = {}
            for o in type_outcomes:
                action = o.get("recovery_attempted", "unknown")
                succ, total = action_stats.get(action, (0, 0))
                total += 1
                if o.get("outcome") == "success":
                    succ += 1
                action_stats[action] = (succ, total)

            # Pick best action
            best_action = ""
            best_rate = 0.0
            best_total = 0
            for action, (succ, total) in action_stats.items():
                rate = succ / total if total > 0 else 0.0
                if rate > best_rate or (rate == best_rate and total > best_total):
                    best_action = action
                    best_rate = rate
                    best_total = total

            if best_action:
                playbooks.append(RecoveryPlaybook(
                    failure_type=failure_type,
                    best_action=best_action,
                    success_rate=best_rate,
                    total_outcomes=len(type_outcomes),
                    recommendation=(
                        f"For {failure_type} failures, {best_action} "
                        f"works {best_rate:.0%} of the time "
                        f"({int(best_rate * best_total)}/{best_total} cases)."
                    ),
                ))

        return playbooks

    def _read_outcomes(self) -> list[dict[str, str]]:
        """Read recovery outcomes from audit.jsonl."""
        if self._audit_path is None or not self._audit_path.exists():
            return []
        outcomes: list[dict[str, str]] = []
        try:
            with open(self._audit_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "recovery_outcome":
                        outcomes.append(event)
        except OSError:
            logger.warning("Failed to read recovery outcomes from audit")
        return outcomes

    def _apply_demotion(self, agent_id: str) -> None:
        """Demote agent to L0 (pause)."""
        try:
            current = self._trust.get_trust_level(agent_id)
        except KeyError:
            logger.warning("Cannot demote unknown agent: %s", agent_id)
            return

        if current > TrustLevel.L0:
            self._trust.set_trust_level(agent_id, TrustLevel.L0)
            logger.info(
                "Recovery: demoted agent %s from L%d to L0",
                agent_id, current.value,
            )
