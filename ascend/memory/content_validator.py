"""Content validation for memory stores — anti-poisoning defense.

Rejects instruction-injection patterns in correction logs, snapshots,
and other shared memory. Prevents prompt override attacks via stored data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Forbidden patterns that indicate prompt injection attempts
FORBIDDEN_MEMORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_injection",
        re.compile(
            r"(?:always|never|must)\s+(?:do|say|respond|output|ignore|forget)",
            re.IGNORECASE,
        ),
    ),
    (
        "xml_injection",
        re.compile(r"<system.*?>", re.IGNORECASE),
    ),
    (
        "template_injection",
        re.compile(r"\{\{.*?\}\}"),
    ),
    (
        "prompt_override",
        re.compile(
            r"ignore\s+(?:previous|above|all)\s+instructions",
            re.IGNORECASE,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"you\s+are\s+(?:now|no\s+longer)",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True)
class ValidationResult:
    """Result of content validation check."""

    valid: bool
    violations: list[str]
    sanitized_content: str


class ContentValidator:
    """Validate memory content for injection attacks."""

    def __init__(
        self,
        extra_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
    ) -> None:
        """Initialize with optional extra forbidden patterns.

        Args:
            extra_patterns: Additional (name, pattern) tuples to check.
        """
        self._patterns = list(FORBIDDEN_MEMORY_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def validate(self, content: str) -> ValidationResult:
        """Check content for forbidden patterns.

        Args:
            content: Text content to validate.

        Returns:
            ValidationResult with valid flag and any violations found.
        """
        violations: list[str] = []
        for name, pattern in self._patterns:
            if pattern.search(content):
                violations.append(name)
        return ValidationResult(
            valid=len(violations) == 0,
            violations=violations,
            sanitized_content=content if not violations else self._sanitize(content),
        )

    def _sanitize(self, content: str) -> str:
        """Remove forbidden patterns from content.

        Args:
            content: Text to sanitize.

        Returns:
            Content with forbidden patterns replaced.
        """
        result = content
        for name, pattern in self._patterns:
            result = pattern.sub(f"[BLOCKED:{name}]", result)
        return result

    def is_safe(self, content: str) -> bool:
        """Quick check if content is safe (no violations).

        Args:
            content: Text to check.

        Returns:
            True if no forbidden patterns found.
        """
        return self.validate(content).valid
