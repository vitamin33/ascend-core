"""PromptEvolver — extract correction log patterns into append-only prompt rules.

Reads data/corrections/{agent_id}.log, groups repeated patterns,
and writes MANDATORY rules to data/prompt_rules/{agent_id}.yaml.
Rules are injected into agent prompts by context_builder.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_ASCEND_ROOT = Path(__file__).parent.parent
_CORRECTIONS_DIR = _ASCEND_ROOT / "data" / "corrections"
_RULES_DIR = _ASCEND_ROOT / "data" / "prompt_rules"

# Keywords that indicate actionable correction entries
_PATTERN_KEYWORDS = {
    "REJECTED", "CORRECTION", "PATTERN", "INSIGHT",
    "FIX", "IMPROVE", "MANDATORY", "AVOID",
}

# Minimum occurrences before a pattern becomes a rule
_MIN_OCCURRENCES = 2

# Maximum rules per agent to avoid prompt bloat
_MAX_RULES_PER_AGENT = 10


@dataclass
class CorrectionEntry:
    """A parsed correction log entry."""

    timestamp: str
    category: str
    content: str


@dataclass
class ExtractedRule:
    """A rule extracted from correction patterns."""

    rule: str
    source_count: int
    source_examples: list[str] = field(default_factory=list)


class PromptEvolver:
    """Extract correction log patterns and generate prompt rules."""

    def __init__(
        self,
        corrections_dir: Path | None = None,
        rules_dir: Path | None = None,
    ) -> None:
        """Initialize with paths to corrections and rules directories.

        Args:
            corrections_dir: Path to correction logs. Defaults to data/corrections/.
            rules_dir: Path to write rules. Defaults to data/prompt_rules/.
        """
        self._corrections_dir = corrections_dir or _CORRECTIONS_DIR
        self._rules_dir = rules_dir or _RULES_DIR

    def evolve(self, agent_id: str) -> list[str]:
        """Read correction log and generate rules for one agent.

        Args:
            agent_id: Agent identifier (matches correction log filename).

        Returns:
            List of rule strings written to the agent's rules file.
        """
        entries = self._parse_corrections(agent_id)
        if not entries:
            logger.info("No corrections found for %s", agent_id)
            return []

        patterns = self._extract_patterns(entries)
        rules = self._patterns_to_rules(patterns)

        if rules:
            self._write_rules(agent_id, rules)
            logger.info(
                "Generated %d rules for %s from %d corrections",
                len(rules), agent_id, len(entries),
            )

        return [r.rule for r in rules]

    def evolve_all(self) -> dict[str, list[str]]:
        """Run evolve for all agents with correction logs.

        Returns:
            Dict mapping agent_id to list of generated rules.
        """
        results: dict[str, list[str]] = {}
        if not self._corrections_dir.exists():
            return results

        for log_file in self._corrections_dir.glob("*.log"):
            agent_id = log_file.stem
            if agent_id.startswith("."):
                continue
            rules = self.evolve(agent_id)
            if rules:
                results[agent_id] = rules

        return results

    def get_rules(self, agent_id: str) -> list[str]:
        """Load existing rules for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            List of rule strings, empty if no rules file exists.
        """
        rules_path = self._rules_dir / f"{agent_id}.yaml"
        if not rules_path.exists():
            return []
        try:
            with open(rules_path) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                raw_rules = data.get("rules", [])
                if isinstance(raw_rules, list):
                    return [str(r) for r in raw_rules]
            return []
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to load rules for %s: %s", agent_id, exc)
            return []

    def get_stats(self, agent_id: str) -> dict[str, object]:
        """Get correction log stats for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            Dict with total_entries, categories, rules_count.
        """
        entries = self._parse_corrections(agent_id)
        rules = self.get_rules(agent_id)

        categories: Counter[str] = Counter()
        for entry in entries:
            categories[entry.category] += 1

        return {
            "agent_id": agent_id,
            "total_entries": len(entries),
            "categories": dict(categories),
            "rules_count": len(rules),
            "rules": rules,
        }

    def _parse_corrections(self, agent_id: str) -> list[CorrectionEntry]:
        """Parse a correction log file into structured entries."""
        log_path = self._corrections_dir / f"{agent_id}.log"
        if not log_path.exists():
            return []

        entries: list[CorrectionEntry] = []
        try:
            lines = log_path.read_text().strip().split("\n")
        except OSError:
            return []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            entry = self._parse_line(line)
            if entry:
                entries.append(entry)

        return entries

    def _parse_line(self, line: str) -> CorrectionEntry | None:
        """Parse a single correction log line.

        Supports formats:
          [TIMESTAMP] CATEGORY: content
          YYYY-MM-DD: [CATEGORY] content
        """
        # Format: [2026-03-01T08:38:42.313617+00:00] REJECTED: Too generic
        ts_match = re.match(
            r"\[([^\]]+)\]\s+(\w+):\s*(.*)", line,
        )
        if ts_match:
            return CorrectionEntry(
                timestamp=ts_match.group(1),
                category=ts_match.group(2).upper(),
                content=ts_match.group(3).strip(),
            )

        # Format: 2026-03-01: [PATTERN] description
        alt_match = re.match(
            r"(\d{4}-\d{2}-\d{2}):\s*\[(\w+)\]\s*(.*)", line,
        )
        if alt_match:
            return CorrectionEntry(
                timestamp=alt_match.group(1),
                category=alt_match.group(2).upper(),
                content=alt_match.group(3).strip(),
            )

        return None

    def _extract_patterns(
        self, entries: list[CorrectionEntry],
    ) -> dict[str, int]:
        """Group similar corrections by normalized content.

        Returns:
            Dict mapping normalized pattern to occurrence count.
        """
        normalized: Counter[str] = Counter()
        for entry in entries:
            key = self._normalize(entry.content)
            if key:
                normalized[key] += 1
        return dict(normalized)

    def _patterns_to_rules(
        self, patterns: dict[str, int],
    ) -> list[ExtractedRule]:
        """Convert patterns with sufficient occurrences into rules."""
        rules: list[ExtractedRule] = []

        for pattern, count in sorted(
            patterns.items(), key=lambda x: x[1], reverse=True,
        ):
            if count < _MIN_OCCURRENCES:
                continue
            if len(rules) >= _MAX_RULES_PER_AGENT:
                break

            rule_text = self._pattern_to_rule_text(pattern, count)
            if rule_text:
                rules.append(ExtractedRule(
                    rule=rule_text,
                    source_count=count,
                    source_examples=[pattern],
                ))

        return rules

    def _pattern_to_rule_text(self, pattern: str, count: int) -> str:
        """Convert a normalized pattern into an actionable rule string."""
        lower = pattern.lower()

        if "generic" in lower:
            return (
                "MANDATORY: Include specific numbers, metrics, data points, "
                "and concrete examples — never use generic statements "
                f"(corrected {count}x)"
            )
        if "long" in lower or "length" in lower:
            return (
                "MANDATORY: Keep output concise — respect platform character "
                f"limits, cut filler content (corrected {count}x)"
            )
        if "format" in lower or "structure" in lower:
            return (
                "MANDATORY: Follow the exact output format specified in the "
                f"prompt — headers, sections, structure (corrected {count}x)"
            )
        if "missing" in lower or "incomplete" in lower:
            return (
                "MANDATORY: Include ALL required sections — check the prompt "
                f"requirements before finalizing (corrected {count}x)"
            )
        if "tone" in lower or "voice" in lower:
            return (
                "MANDATORY: Match the specified tone/voice — professional, "
                f"technical, no casual language (corrected {count}x)"
            )
        if "data" in lower or "source" in lower:
            return (
                "MANDATORY: Cite real data sources for every claim — "
                f"no unsupported assertions (corrected {count}x)"
            )

        # Generic fallback for unrecognized patterns
        return f"LEARNED: {pattern} (corrected {count}x)"

    def _write_rules(
        self, agent_id: str, rules: list[ExtractedRule],
    ) -> None:
        """Write rules to YAML file (append-only: merge with existing)."""
        self._rules_dir.mkdir(parents=True, exist_ok=True)
        rules_path = self._rules_dir / f"{agent_id}.yaml"

        existing = self.get_rules(agent_id)
        existing_set = set(existing)

        new_rules = [r.rule for r in rules if r.rule not in existing_set]
        merged = existing + new_rules

        # Cap at max rules
        merged = merged[:_MAX_RULES_PER_AGENT]

        data = {
            "agent_id": agent_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "rules": merged,
        }

        rules_path.write_text(yaml.dump(data, default_flow_style=False))
        logger.info(
            "Wrote %d rules for %s (%d new)",
            len(merged), agent_id, len(new_rules),
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize correction text for grouping."""
        text = text.strip().lower()
        text = re.sub(r"\s+", " ", text)
        # Remove timestamps and numbers for grouping
        text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
        return text.strip()
