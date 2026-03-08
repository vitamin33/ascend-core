"""Entity memory — track named entities across agent runs.

Entities: people, repos, services, technologies, clients.
Each entity has typed facts with confidence, source attribution, and project scoping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class EntityFact:
    """A single fact about an entity."""

    fact: str
    confidence: float
    source: str
    date: str


@dataclass
class Entity:
    """A named entity with typed facts."""

    entity_id: int
    name: str
    entity_type: str
    project: str | None
    facts: list[EntityFact]
    confidence: float
    source_agent: str
    created_at: str
    updated_at: str
    aliases: list[str] = field(default_factory=list)


class EntityStore:
    """SQLite-backed entity memory with fact tracking and alias resolution."""

    def __init__(self, db_path: Path) -> None:
        """Initialize entity store with SQLite database.

        Args:
            db_path: Path to knowledge.db or dedicated entity database.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        """Create entities and aliases tables."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                project TEXT,
                facts_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL DEFAULT 0.8,
                source_agent TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name, entity_type, project)
            );

            CREATE INDEX IF NOT EXISTS idx_entities_project
                ON entities(project);
            CREATE INDEX IF NOT EXISTS idx_entities_type
                ON entities(entity_type);

            CREATE TABLE IF NOT EXISTS entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                FOREIGN KEY (entity_id) REFERENCES entities(id)
                    ON DELETE CASCADE,
                UNIQUE(alias)
            );

            CREATE INDEX IF NOT EXISTS idx_aliases_entity
                ON entity_aliases(entity_id);
        """)
        self._conn.commit()

    def upsert_entity(
        self,
        name: str,
        entity_type: str,
        project: str | None = None,
        facts: list[EntityFact] | None = None,
        confidence: float = 0.8,
        source_agent: str = "",
    ) -> int:
        """Create or update an entity.

        Args:
            name: Entity name (canonical).
            entity_type: Type: person, repo, service, technology, client.
            project: Project scope (None = cross-project).
            facts: Initial facts about this entity.
            confidence: Overall confidence score.
            source_agent: Agent that discovered this entity.

        Returns:
            Entity ID.
        """
        now = datetime.now(UTC).isoformat()
        facts_json = json.dumps(
            [{"fact": f.fact, "confidence": f.confidence,
              "source": f.source, "date": f.date}
             for f in (facts or [])],
        )

        existing = self._conn.execute(
            "SELECT id, facts_json FROM entities "
            "WHERE name = ? AND entity_type = ? AND project IS ?",
            (name, entity_type, project),
        ).fetchone()

        if existing:
            # Merge facts
            old_facts = json.loads(existing["facts_json"])
            new_facts = json.loads(facts_json)
            merged = self._merge_facts(old_facts, new_facts)
            self._conn.execute(
                "UPDATE entities SET facts_json = ?, confidence = ?, "
                "source_agent = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), confidence, source_agent,
                 now, existing["id"]),
            )
            self._conn.commit()
            return int(existing["id"])

        cursor = self._conn.execute(
            "INSERT INTO entities "
            "(name, entity_type, project, facts_json, confidence, "
            "source_agent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, entity_type, project, facts_json, confidence,
             source_agent, now, now),
        )
        self._conn.commit()
        return cursor.lastrowid or 0

    def add_fact(
        self,
        entity_id: int,
        fact: EntityFact,
    ) -> None:
        """Add a fact to an existing entity.

        Args:
            entity_id: Entity ID.
            fact: Fact to add.
        """
        row = self._conn.execute(
            "SELECT facts_json FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if not row:
            return
        facts = json.loads(row["facts_json"])
        facts.append({
            "fact": fact.fact, "confidence": fact.confidence,
            "source": fact.source, "date": fact.date,
        })
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "UPDATE entities SET facts_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(facts), now, entity_id),
        )
        self._conn.commit()

    def add_alias(self, entity_id: int, alias: str) -> None:
        """Register an alias for an entity.

        Args:
            entity_id: Entity ID.
            alias: Alternative name for this entity.
        """
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) "
                "VALUES (?, ?)",
                (entity_id, alias),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            logger.debug("Alias already exists: %s", alias)

    def resolve(self, name: str) -> Entity | None:
        """Resolve a name or alias to an entity.

        Args:
            name: Entity name or alias to look up.

        Returns:
            Entity if found, None otherwise.
        """
        # Direct name match first
        row = self._conn.execute(
            "SELECT * FROM entities WHERE name = ?",
            (name,),
        ).fetchone()

        # Try alias lookup
        if not row:
            alias_row = self._conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ?",
                (name,),
            ).fetchone()
            if alias_row:
                row = self._conn.execute(
                    "SELECT * FROM entities WHERE id = ?",
                    (alias_row["entity_id"],),
                ).fetchone()

        if not row:
            return None
        return self._row_to_entity(row)

    def search_by_project(
        self,
        project: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        """Search entities by project and/or type.

        Args:
            project: Filter by project (None = cross-project only).
            entity_type: Filter by entity type.
            limit: Maximum results.

        Returns:
            List of matching entities.
        """
        conditions: list[str] = []
        params: list[object] = []

        if project is not None:
            conditions.append("(project = ? OR project IS NULL)")
            params.append(project)
        if entity_type is not None:
            conditions.append("entity_type = ?")
            params.append(entity_type)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM entities WHERE {where} "  # noqa: S608
            "ORDER BY updated_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()

        return [self._row_to_entity(r) for r in rows]

    def get_for_injection(
        self,
        project: str,
        max_entities: int = 10,
        min_confidence: float = 0.4,
    ) -> str:
        """Get entity context for injection into agent prompts.

        Args:
            project: Current project scope.
            max_entities: Maximum entities to include.
            min_confidence: Minimum confidence threshold.

        Returns:
            Formatted entity context or empty string.
        """
        rows = self._conn.execute(
            "SELECT * FROM entities "
            "WHERE (project = ? OR project IS NULL) "
            "AND confidence >= ? "
            "ORDER BY confidence DESC, updated_at DESC "
            "LIMIT ?",
            (project, min_confidence, max_entities),
        ).fetchall()

        if not rows:
            return ""

        parts: list[str] = []
        for row in rows:
            facts = json.loads(row["facts_json"])
            fact_lines = [f"  - {f['fact']}" for f in facts[:3]]
            scope = row["project"] or "cross-project"
            parts.append(
                f"[{row['entity_type']}] {row['name']} "
                f"(scope: {scope}, confidence: {row['confidence']:.1f})\n"
                + "\n".join(fact_lines),
            )

        return "## Known Entities\n" + "\n".join(parts)

    def _row_to_entity(self, row: sqlite3.Row) -> Entity:
        """Convert a database row to an Entity dataclass."""
        aliases = self._conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?",
            (row["id"],),
        ).fetchall()
        return Entity(
            entity_id=row["id"],
            name=row["name"],
            entity_type=row["entity_type"],
            project=row["project"],
            facts=[
                EntityFact(**f)
                for f in json.loads(row["facts_json"])
            ],
            confidence=row["confidence"],
            source_agent=row["source_agent"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            aliases=[a["alias"] for a in aliases],
        )

    @staticmethod
    def _merge_facts(
        old: list[dict[str, object]],
        new: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Merge fact lists, deduplicating by fact text."""
        seen = {str(f.get("fact", "")) for f in old}
        merged = list(old)
        for fact in new:
            fact_text = str(fact.get("fact", ""))
            if fact_text not in seen:
                merged.append(fact)
                seen.add(fact_text)
        return merged

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
