"""Garbage collector for memory stores.

Runs daily/weekly/monthly cleanup based on config/retention.yaml.
Wired into daemon startup for daily cleanup.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import yaml

from ascend.memory.messages import AgentMessageQueue

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GarbageCollector:
    """Memory garbage collector with configurable retention policies."""

    def __init__(
        self,
        data_dir: Path,
        retention_path: Path | None = None,
    ) -> None:
        """Initialize garbage collector.

        Args:
            data_dir: Root data directory.
            retention_path: Path to retention.yaml config.
        """
        self._data_dir = data_dir
        self._config = self._load_config(retention_path)

    @staticmethod
    def _load_config(path: Path | None) -> dict[str, dict[str, object]]:
        """Load retention config from YAML."""
        if path and path.exists():
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict):
                    stores = data.get("stores", {})
                    if isinstance(stores, dict):
                        return stores
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("Failed to load retention config: %s", exc)
        return {}

    def run_daily(self) -> dict[str, int]:
        """Run lightweight daily cleanup.

        Returns:
            Dict of store_name -> items_cleaned.
        """
        results: dict[str, int] = {}

        # Clean expired messages
        msg_config = self._config.get("messages", {})
        if isinstance(msg_config, dict):
            msg_dir = self._data_dir / "messages"
            if msg_dir.exists():
                queue = AgentMessageQueue(msg_dir)
                cleaned = queue.cleanup_expired()
                results["messages"] = cleaned

        # Clean old run logs
        run_config = self._config.get("run_logs", {})
        if isinstance(run_config, dict):
            retention_days = int(str(run_config.get("retention_days", 30)))
            cleaned = self._clean_run_logs(retention_days)
            results["run_logs"] = cleaned

        logger.info("Daily GC complete: %s", results)
        return results

    def run_weekly(self) -> dict[str, int]:
        """Run moderate weekly cleanup.

        Returns:
            Dict of store_name -> items_cleaned.
        """
        results = self.run_daily()

        # Trim correction logs
        corr_config = self._config.get("corrections", {})
        if isinstance(corr_config, dict):
            max_lines = int(str(corr_config.get("max_lines_per_agent", 50)))
            cleaned = self._trim_corrections(max_lines)
            results["corrections"] = cleaned

        # Clean snapshot metadata for deleted snapshots
        cleaned = self._clean_snapshot_meta()
        results["snapshot_meta"] = cleaned

        logger.info("Weekly GC complete: %s", results)
        return results

    def run_monthly(self) -> dict[str, int]:
        """Run full monthly cleanup.

        Returns:
            Dict of store_name -> items_cleaned.
        """
        results = self.run_weekly()

        # Trim old trends
        trend_config = self._config.get("trends", {})
        if isinstance(trend_config, dict):
            keep_weeks = int(str(trend_config.get("keep_weeks", 12)))
            cleaned = self._trim_trends(keep_weeks)
            results["trends"] = cleaned

        logger.info("Monthly GC complete: %s", results)
        return results

    def _clean_run_logs(self, retention_days: int) -> int:
        """Remove run logs older than retention_days."""
        runs_dir = self._data_dir.parent / "logs" / "runs"
        if not runs_dir.exists():
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        removed = 0
        for path in runs_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC,
                )
                if mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _trim_corrections(self, max_lines: int) -> int:
        """Trim correction logs to max_lines per agent."""
        corr_dir = self._data_dir / "corrections"
        if not corr_dir.exists():
            return 0

        trimmed = 0
        for path in corr_dir.glob("*.log"):
            try:
                lines = path.read_text().strip().split("\n")
                if len(lines) > max_lines:
                    trimmed += len(lines) - max_lines
                    path.write_text("\n".join(lines[-max_lines:]) + "\n")
            except OSError:
                continue
        return trimmed

    def _clean_snapshot_meta(self) -> int:
        """Remove metadata entries for snapshots that no longer exist."""
        snap_dir = self._data_dir / "snapshots"
        meta_path = snap_dir / "_metadata.json"
        if not meta_path.exists():
            return 0

        try:
            meta = json.loads(meta_path.read_text())
            if not isinstance(meta, dict):
                return 0

            to_remove = []
            for name in meta:
                snap_file = snap_dir / f"{name}.json"
                if not snap_file.exists():
                    to_remove.append(name)

            for name in to_remove:
                del meta[name]

            if to_remove:
                meta_path.write_text(json.dumps(meta, indent=2))
            return len(to_remove)
        except (json.JSONDecodeError, OSError):
            return 0

    def _trim_trends(self, keep_weeks: int) -> int:
        """Remove trend files older than keep_weeks."""
        trends_dir = self._data_dir / "trends"
        if not trends_dir.exists():
            return 0

        files = sorted(trends_dir.glob("week_*.json"))
        if len(files) <= keep_weeks:
            return 0

        to_remove = files[:-keep_weeks]
        for path in to_remove:
            try:
                path.unlink()
            except OSError:
                continue
        return len(to_remove)
