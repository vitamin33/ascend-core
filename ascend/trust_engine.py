"""Trust engine with SQLite-backed trust levels L0-L4."""

from __future__ import annotations

import enum
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TrustLevel(enum.IntEnum):
    """Agent trust levels from L0 (read-only) to L4 (full autonomy)."""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


class TrustDecision(enum.Enum):
    """Possible outcomes of a trust-level check."""

    READ_ONLY = "read_only"
    REQUIRES_APPROVAL = "requires_approval"
    AUTO_EXECUTE = "auto_execute"


def trust_level_from_string(level_str: str) -> TrustLevel:
    """Convert 'L0'–'L4' to a TrustLevel enum value.

    Raises ValueError for unrecognised strings.
    """
    mapping: dict[str, TrustLevel] = {
        f"L{lvl.value}": lvl for lvl in TrustLevel
    }
    if level_str not in mapping:
        raise ValueError(
            f"Invalid trust level string: '{level_str}'. "
            f"Expected one of {sorted(mapping)}"
        )
    return mapping[level_str]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMOTION_RUNS_REQUIRED = 10
_DEMOTION_FAILURE_THRESHOLD = 2
_DEMOTION_WINDOW_HOURS = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_str() -> str:
    """Return current UTC time in a format SQLite can compare."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _utc_cutoff_str(hours: int) -> str:
    """Return a UTC timestamp N hours ago, SQLite-comparable."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# TrustEngine
# ---------------------------------------------------------------------------


class TrustEngine:
    """SQLite-backed trust engine for agent permission management."""

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the trust database at *db_path*."""
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    # -- schema -------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create tables if they do not already exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_id       TEXT PRIMARY KEY,
                project        TEXT NOT NULL DEFAULT '',
                trust_level    INTEGER NOT NULL DEFAULT 0,
                registered_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   TEXT NOT NULL,
                task_id    TEXT NOT NULL,
                status     TEXT NOT NULL,
                error      TEXT NOT NULL DEFAULT '',
                project    TEXT NOT NULL DEFAULT '',
                agent_cli  TEXT NOT NULL DEFAULT '',
                timestamp  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trust_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id    TEXT NOT NULL,
                from_level  INTEGER NOT NULL,
                to_level    INTEGER NOT NULL,
                reason      TEXT NOT NULL,
                timestamp   TEXT NOT NULL
            );
        """)
        self._conn.commit()
        self._migrate_agent_runs()

    def _migrate_agent_runs(self) -> None:
        """Add missing columns to agent_runs (migration)."""
        cursor = self._conn.execute("PRAGMA table_info(agent_runs)")
        columns = {row[1] for row in cursor.fetchall()}
        if "project" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_runs ADD COLUMN project TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()
        if "agent_cli" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_runs ADD COLUMN agent_cli TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()

    # -- registration -------------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        project: str = "",
        fast_lane_level: TrustLevel = TrustLevel.L0,
    ) -> None:
        """Register a new agent, optionally with a fast-lane starting level."""
        self._conn.execute(
            "INSERT INTO agents (agent_id, project, trust_level, registered_at) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, project, fast_lane_level.value, _utc_now_str()),
        )
        self._conn.commit()

    def get_or_register_agent(
        self,
        agent_id: str,
        project: str = "",
    ) -> TrustLevel:
        """Return trust level for agent, auto-registering at L0 if unknown."""
        row = self._conn.execute(
            "SELECT trust_level FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is not None:
            return TrustLevel(row[0])

        self.register_agent(agent_id, project=project)
        return TrustLevel.L0

    def list_agents(self) -> list[dict[str, object]]:
        """Return all registered agents with trust level and run count."""
        cursor = self._conn.execute(
            "SELECT a.agent_id, a.project, a.trust_level, "
            "COUNT(r.id) AS total_runs "
            "FROM agents a "
            "LEFT JOIN agent_runs r ON a.agent_id = r.agent_id "
            "GROUP BY a.agent_id "
            "ORDER BY a.agent_id",
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]

    def check_connection(self) -> bool:
        """Return True if the database is reachable."""
        try:
            self._conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    # -- trust level accessors ----------------------------------------------

    def get_trust_level(self, agent_id: str) -> TrustLevel:
        """Return the current trust level for *agent_id*."""
        row = self._conn.execute(
            "SELECT trust_level FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        return TrustLevel(row[0])

    def set_trust_level(self, agent_id: str, level: TrustLevel) -> None:
        """Directly set an agent's trust level (logs the change)."""
        old = self.get_trust_level(agent_id)
        self._conn.execute(
            "UPDATE agents SET trust_level = ? WHERE agent_id = ?",
            (level.value, agent_id),
        )
        self._log_level_change(agent_id, old, level, "manual set")
        self._conn.commit()

    # -- run logging --------------------------------------------------------

    def log_agent_run(
        self,
        agent_id: str,
        task_id: str,
        status: str,
        error: str,
        project: str = "",
        agent_cli: str = "",
    ) -> None:
        """Record an agent run in the database."""
        self._conn.execute(
            "INSERT INTO agent_runs "
            "(agent_id, task_id, status, error, project, agent_cli, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, task_id, status, error, project, agent_cli, _utc_now_str()),
        )
        self._conn.commit()

    def get_recent_runs(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Return the most recent runs for an agent as a list of dicts."""
        cursor = self._conn.execute(
            "SELECT agent_id, task_id, status, error, project, agent_cli, timestamp "
            "FROM agent_runs WHERE agent_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (agent_id, limit),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]

    def get_project_runs(
        self,
        agent_id: str,
        project: str,
        limit: int = 10,
    ) -> list[dict[str, str]]:
        """Return the most recent runs for an agent filtered by project.

        Args:
            agent_id: The agent to query.
            project: The project to filter by.
            limit: Maximum number of runs to return.

        Returns:
            List of run dicts ordered by most recent first.
        """
        cursor = self._conn.execute(
            "SELECT agent_id, task_id, status, error, project, agent_cli, timestamp "
            "FROM agent_runs WHERE agent_id = ? AND project = ? "
            "ORDER BY id DESC LIMIT ?",
            (agent_id, project, limit),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]

    def evaluate_project_promotion(
        self,
        agent_id: str,
        project: str,
    ) -> None:
        """Promote agent if 10 consecutive successes in a specific project.

        Args:
            agent_id: The agent to evaluate.
            project: The project to scope the evaluation to.
        """
        current = self.get_trust_level(agent_id)
        if current >= TrustLevel.L4:
            return

        runs = self.get_project_runs(
            agent_id, project, limit=_PROMOTION_RUNS_REQUIRED,
        )
        if len(runs) < _PROMOTION_RUNS_REQUIRED:
            return
        if not all(r["status"] == "success" for r in runs):
            return

        new = TrustLevel(current.value + 1)
        self._conn.execute(
            "UPDATE agents SET trust_level = ? WHERE agent_id = ?",
            (new.value, agent_id),
        )
        self._log_level_change(
            agent_id, current, new,
            f"project promotion ({project}): "
            f"{_PROMOTION_RUNS_REQUIRED} consecutive successes",
        )
        self._conn.commit()

    # -- promotion / demotion -----------------------------------------------

    def evaluate_promotion(
        self,
        agent_id: str,
        project_max_trust: int | None = None,
    ) -> None:
        """Promote the agent by one level if criteria are met."""
        current = self.get_trust_level(agent_id)

        if current >= TrustLevel.L4:
            return

        if project_max_trust is not None and current.value >= project_max_trust:
            return

        runs = self.get_recent_runs(agent_id, limit=_PROMOTION_RUNS_REQUIRED)
        if len(runs) < _PROMOTION_RUNS_REQUIRED:
            return
        if not all(r["status"] == "success" for r in runs):
            return

        new = TrustLevel(current.value + 1)
        self._conn.execute(
            "UPDATE agents SET trust_level = ? WHERE agent_id = ?",
            (new.value, agent_id),
        )
        self._log_level_change(
            agent_id, current, new,
            f"promotion: {_PROMOTION_RUNS_REQUIRED} consecutive successes",
        )
        self._conn.commit()

    def evaluate_demotion(self, agent_id: str) -> None:
        """Demote the agent by one level if failure threshold is exceeded."""
        current = self.get_trust_level(agent_id)
        if current <= TrustLevel.L0:
            return

        cutoff = _utc_cutoff_str(_DEMOTION_WINDOW_HOURS)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM agent_runs "
            "WHERE agent_id = ? AND status = 'failure' "
            "AND timestamp >= ?",
            (agent_id, cutoff),
        ).fetchone()
        failure_count: int = row[0]

        if failure_count >= _DEMOTION_FAILURE_THRESHOLD:
            new = TrustLevel(current.value - 1)
            self._conn.execute(
                "UPDATE agents SET trust_level = ? WHERE agent_id = ?",
                (new.value, agent_id),
            )
            self._log_level_change(
                agent_id, current, new,
                f"demotion: {failure_count} failures in {_DEMOTION_WINDOW_HOURS}h",
            )
            self._conn.commit()

    # -- decision -----------------------------------------------------------

    def decide(
        self,
        agent_id: str,
        action: str,
    ) -> TrustDecision:
        """Map the agent's trust level to an execution decision.

        READ_ONLY is an audit-mode indicator, not a hard block.
        The executor logs it but proceeds with execution.
        """
        level = self.get_trust_level(agent_id)
        if level <= TrustLevel.L0:
            return TrustDecision.READ_ONLY
        if level == TrustLevel.L1:
            return TrustDecision.REQUIRES_APPROVAL
        return TrustDecision.AUTO_EXECUTE

    # -- project-scoped trust -----------------------------------------------

    def get_project_trust(
        self,
        agent_id: str,
        project: str,
    ) -> TrustLevel:
        """Get effective trust level for an agent in a specific project.

        Uses project-specific run history to determine if the agent has
        earned trust in that project context. Falls back to global level.

        Args:
            agent_id: The agent to check.
            project: The project context.

        Returns:
            Effective trust level for this agent+project combination.
        """
        global_level = self.get_or_register_agent(agent_id, project=project)

        # Check project-specific success history
        project_runs = self.get_project_runs(agent_id, project, limit=20)
        if not project_runs:
            # No project history — use global level but cap at L1
            return min(global_level, TrustLevel.L1)

        # Count recent project failures
        recent_failures = sum(
            1 for r in project_runs[:10] if r["status"] in ("failure", "timeout")
        )
        if recent_failures >= _DEMOTION_FAILURE_THRESHOLD:
            return max(TrustLevel.L0, TrustLevel(global_level.value - 1))

        # Agent has project history — use global level
        return global_level

    def get_project_stats(
        self,
        agent_id: str,
        project: str,
    ) -> dict[str, object]:
        """Get trust statistics for an agent in a specific project.

        Args:
            agent_id: The agent.
            project: The project.

        Returns:
            Dict with trust stats for the agent+project pair.
        """
        runs = self.get_project_runs(agent_id, project, limit=50)
        total = len(runs)
        successes = sum(1 for r in runs if r["status"] == "success")
        failures = sum(1 for r in runs if r["status"] in ("failure", "timeout"))
        return {
            "agent_id": agent_id,
            "project": project,
            "global_trust": self.get_or_register_agent(agent_id).value,
            "project_trust": self.get_project_trust(agent_id, project).value,
            "total_runs": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / total * 100, 1) if total > 0 else 0.0,
        }

    # -- internal helpers ---------------------------------------------------

    def _log_level_change(
        self,
        agent_id: str,
        old: TrustLevel,
        new: TrustLevel,
        reason: str,
    ) -> None:
        """Insert a row into trust_log."""
        self._conn.execute(
            "INSERT INTO trust_log "
            "(agent_id, from_level, to_level, reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, old.value, new.value, reason, _utc_now_str()),
        )
