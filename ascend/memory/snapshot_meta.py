"""Snapshot metadata tracking for staleness detection.

Every snapshot gets metadata: generated_at, source_agent, confidence, tokens.
Context builder uses this to warn about stale data and allocate budget.
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

_STALE_THRESHOLD_DAYS = 7


@dataclass
class SnapshotMeta:
    """Metadata for a single snapshot file."""

    name: str
    generated_at: str
    source_agent: str
    project: str
    confidence: float
    token_count: int
    version: int = 1

    def age_days(self) -> float:
        """Return age in days from generated_at to now."""
        try:
            ts = datetime.fromisoformat(self.generated_at)
            delta = datetime.now(UTC) - ts
            return delta.total_seconds() / 86400.0
        except (ValueError, TypeError):
            return 999.0  # Unknown age = very stale

    def is_stale(self, threshold_days: float = _STALE_THRESHOLD_DAYS) -> bool:
        """Check if snapshot is older than threshold."""
        return self.age_days() > threshold_days

    def staleness_label(self) -> str:
        """Return human-readable staleness label."""
        age = self.age_days()
        if age < 1.0:
            return "fresh"
        if age < 7.0:
            return "aging"
        if age < 30.0:
            return "stale"
        return "critical"

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SnapshotMeta:
        """Deserialize from dict."""
        return cls(
            name=str(data.get("name", "")),
            generated_at=str(data.get("generated_at", "")),
            source_agent=str(data.get("source_agent", "")),
            project=str(data.get("project", "all")),
            confidence=float(str(data.get("confidence", 0.8))),
            token_count=int(str(data.get("token_count", 0))),
            version=int(str(data.get("version", 1))),
        )


class SnapshotMetaRegistry:
    """Tracks metadata for all snapshots in data/snapshots/.

    Reads _metadata.json or infers metadata from snapshot files.
    """

    def __init__(self, snapshot_dir: Path) -> None:
        """Initialize registry.

        Args:
            snapshot_dir: Path to data/snapshots/ directory.
        """
        self._snapshot_dir = snapshot_dir
        self._meta_path = snapshot_dir / "_metadata.json"
        self._cache: dict[str, SnapshotMeta] | None = None

    def _load(self) -> dict[str, SnapshotMeta]:
        """Load metadata from file or build from snapshots."""
        if self._cache is not None:
            return self._cache

        meta: dict[str, SnapshotMeta] = {}

        # Try loading stored metadata
        if self._meta_path.exists():
            try:
                raw = json.loads(self._meta_path.read_text())
                if isinstance(raw, dict):
                    for name, data in raw.items():
                        if isinstance(data, dict):
                            meta[name] = SnapshotMeta.from_dict(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load snapshot metadata: %s", exc)

        # Fill gaps from snapshot files themselves
        if self._snapshot_dir.exists():
            for path in self._snapshot_dir.glob("*.json"):
                if path.name.startswith("_"):
                    continue
                name = path.stem
                if name not in meta:
                    meta[name] = self._infer_meta(name, path)

        self._cache = meta
        return meta

    def _infer_meta(self, name: str, path: Path) -> SnapshotMeta:
        """Infer metadata from a snapshot file's content and mtime."""
        try:
            data = json.loads(path.read_text())
            written_at = str(data.get("_snapshot_written_at", ""))
            content = json.dumps(data)
            token_count = len(content) // 4
        except (json.JSONDecodeError, OSError):
            written_at = ""
            token_count = 0

        if not written_at:
            # Fall back to file mtime
            try:
                mtime = path.stat().st_mtime
                written_at = datetime.fromtimestamp(
                    mtime, tz=UTC,
                ).isoformat()
            except OSError:
                written_at = datetime.now(UTC).isoformat()

        return SnapshotMeta(
            name=name,
            generated_at=written_at,
            source_agent=name,
            project="all",
            confidence=0.8,
            token_count=token_count,
        )

    def get(self, name: str) -> SnapshotMeta | None:
        """Get metadata for a specific snapshot."""
        return self._load().get(name)

    def get_all(self) -> dict[str, SnapshotMeta]:
        """Get all snapshot metadata."""
        return dict(self._load())

    def update(self, meta: SnapshotMeta) -> None:
        """Update metadata for a snapshot and persist."""
        loaded = self._load()
        loaded[meta.name] = meta
        self._save(loaded)

    def _save(self, meta: dict[str, SnapshotMeta]) -> None:
        """Persist metadata to _metadata.json."""
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        data = {name: m.to_dict() for name, m in meta.items()}
        self._meta_path.write_text(json.dumps(data, indent=2))
        self._cache = meta

    def invalidate_cache(self) -> None:
        """Clear cached metadata to force reload."""
        self._cache = None
