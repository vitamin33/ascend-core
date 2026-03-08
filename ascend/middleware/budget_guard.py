"""Budget guard middleware — tracks API call costs and enforces limits."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetStatus:
    """Result of a budget check."""

    allowed: bool
    daily_used: int
    monthly_used: int
    reason: str = ""


def _utc_now_str() -> str:
    """Return current UTC time in SQLite-comparable format."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _utc_cutoff_str(hours: int) -> str:
    """Return a UTC timestamp N hours ago, SQLite-comparable."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


class BudgetGuard:
    """Tracks API call costs in SQLite and enforces daily/monthly limits."""

    def __init__(
        self,
        db_path: Path,
        daily_limit: int,
        monthly_limit: int,
    ) -> None:
        """Open or create the budget database."""
        self._db_path = db_path
        self._daily_limit = daily_limit
        self._monthly_limit = monthly_limit
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._create_tables()
        self._migrate_budget_log()

    def _create_tables(self) -> None:
        """Create budget_log table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS budget_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id  TEXT NOT NULL,
                task_id   TEXT NOT NULL,
                cost      INTEGER NOT NULL DEFAULT 1,
                timestamp TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def _migrate_budget_log(self) -> None:
        """Add token/cost columns to budget_log if missing."""
        cursor = self._conn.execute("PRAGMA table_info(budget_log)")
        existing = {row[1] for row in cursor.fetchall()}

        migrations: list[tuple[str, str]] = [
            ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
            ("engine", "TEXT NOT NULL DEFAULT ''"),
            ("duration_seconds", "REAL NOT NULL DEFAULT 0.0"),
            ("estimated_cost_usd", "REAL NOT NULL DEFAULT 0.0"),
        ]

        for col_name, col_def in migrations:
            if col_name not in existing:
                self._conn.execute(
                    f"ALTER TABLE budget_log ADD COLUMN {col_name} {col_def}"
                )
                logger.info("Migrated budget_log: added column %s", col_name)

        self._conn.commit()

    def check_budget(self) -> BudgetStatus:
        """Check if current usage is within daily and monthly limits."""
        daily = self._sum_cost(hours=24)
        monthly = self._sum_cost(hours=24 * 30)

        if daily >= self._daily_limit:
            return BudgetStatus(
                allowed=False,
                daily_used=daily,
                monthly_used=monthly,
                reason=f"Daily limit reached: {daily}/{self._daily_limit}",
            )

        if monthly >= self._monthly_limit:
            return BudgetStatus(
                allowed=False,
                daily_used=daily,
                monthly_used=monthly,
                reason=f"Monthly limit reached: {monthly}/{self._monthly_limit}",
            )

        return BudgetStatus(
            allowed=True,
            daily_used=daily,
            monthly_used=monthly,
        )

    def record_usage(
        self,
        agent_id: str,
        task_id: str,
        cost: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        engine: str = "",
        duration_seconds: float = 0.0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Record an API call in the budget log with optional token data."""
        self._conn.execute(
            "INSERT INTO budget_log "
            "(agent_id, task_id, cost, timestamp, "
            " input_tokens, output_tokens, model, engine, "
            " duration_seconds, estimated_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id, task_id, cost, _utc_now_str(),
                input_tokens, output_tokens, model, engine,
                duration_seconds, estimated_cost_usd,
            ),
        )
        self._conn.commit()

    def get_usage_stats(self) -> dict[str, object]:
        """Return daily/monthly usage counts and per-agent breakdown."""
        daily = self._sum_cost(hours=24)
        monthly = self._sum_cost(hours=24 * 30)

        cutoff = _utc_cutoff_str(hours=24 * 30)
        cursor = self._conn.execute(
            "SELECT agent_id, SUM(cost) FROM budget_log "
            "WHERE timestamp >= ? GROUP BY agent_id",
            (cutoff,),
        )
        per_agent: dict[str, int] = {
            row[0]: row[1] for row in cursor.fetchall()
        }

        # Token totals
        row = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), "
            "       COALESCE(SUM(output_tokens), 0), "
            "       COALESCE(SUM(estimated_cost_usd), 0.0) "
            "FROM budget_log WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        total_input: int = row[0]
        total_output: int = row[1]
        total_cost_usd: float = row[2]

        return {
            "daily_used": daily,
            "monthly_used": monthly,
            "daily_limit": self._daily_limit,
            "monthly_limit": self._monthly_limit,
            "per_agent": per_agent,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost_usd, 4),
        }

    def get_cost_breakdown(self, hours: int = 24 * 30) -> dict[str, object]:
        """Return per-agent + per-model cost breakdown."""
        cutoff = _utc_cutoff_str(hours)

        # Per-agent breakdown
        cursor = self._conn.execute(
            "SELECT agent_id, "
            "       SUM(cost) AS calls, "
            "       SUM(input_tokens) AS inp, "
            "       SUM(output_tokens) AS outp, "
            "       SUM(estimated_cost_usd) AS usd "
            "FROM budget_log WHERE timestamp >= ? "
            "GROUP BY agent_id ORDER BY usd DESC",
            (cutoff,),
        )
        per_agent: list[dict[str, object]] = []
        for row in cursor.fetchall():
            per_agent.append({
                "agent_id": row[0],
                "calls": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
                "estimated_cost_usd": round(float(row[4]), 4),
            })

        # Per-model breakdown
        cursor = self._conn.execute(
            "SELECT model, "
            "       COUNT(*) AS calls, "
            "       SUM(input_tokens) AS inp, "
            "       SUM(output_tokens) AS outp, "
            "       SUM(estimated_cost_usd) AS usd "
            "FROM budget_log WHERE timestamp >= ? AND model != '' "
            "GROUP BY model ORDER BY usd DESC",
            (cutoff,),
        )
        per_model: list[dict[str, object]] = []
        for row in cursor.fetchall():
            per_model.append({
                "model": row[0],
                "calls": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
                "estimated_cost_usd": round(float(row[4]), 4),
            })

        # Totals
        row = self._conn.execute(
            "SELECT COUNT(*), "
            "       COALESCE(SUM(input_tokens), 0), "
            "       COALESCE(SUM(output_tokens), 0), "
            "       COALESCE(SUM(estimated_cost_usd), 0.0) "
            "FROM budget_log WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()

        return {
            "period_hours": hours,
            "total_calls": row[0],
            "total_input_tokens": row[1],
            "total_output_tokens": row[2],
            "total_cost_usd": round(float(row[3]), 4),
            "per_agent": per_agent,
            "per_model": per_model,
        }

    def get_budget_summary_for_context(self) -> str:
        """Return a short budget summary for injection into agent context."""
        stats = self.get_usage_stats()
        daily_used = int(str(stats["daily_used"]))
        monthly_used = int(str(stats["monthly_used"]))
        daily_pct = (
            daily_used / self._daily_limit * 100
        ) if self._daily_limit > 0 else 0
        monthly_pct = (
            monthly_used / self._monthly_limit * 100
        ) if self._monthly_limit > 0 else 0

        # Top 3 agents by cost
        per_agent = stats.get("per_agent", {})
        if isinstance(per_agent, dict):
            sorted_agents = sorted(
                per_agent.items(), key=lambda x: x[1], reverse=True,
            )[:3]
            top_str = ", ".join(
                f"{a}: {c} calls" for a, c in sorted_agents
            )
        else:
            top_str = "none"

        total_usd = stats.get("total_cost_usd", 0.0)

        lines = [
            f"Budget: daily {daily_used}/{self._daily_limit} ({daily_pct:.0f}%), "
            f"monthly {monthly_used}/{self._monthly_limit} ({monthly_pct:.0f}%)",
            f"Total cost: ${total_usd}",
            f"Top agents: {top_str}",
        ]

        # Alert if above threshold
        if daily_pct >= 80 or monthly_pct >= 80:
            lines.insert(0, "WARNING: Budget threshold exceeded (80%)")

        return "\n".join(lines)

    def _sum_cost(self, hours: int) -> int:
        """Sum cost within the given hour window."""
        cutoff = _utc_cutoff_str(hours)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM budget_log "
            "WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        result: int = row[0]
        return result
