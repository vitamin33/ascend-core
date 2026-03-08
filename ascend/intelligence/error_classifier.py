"""Error classification and retry strategies for agent execution.

Classifies exceptions into categories and provides backoff strategies
to replace immediate retries with intelligent delay-based retries.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

try:
    from llm_providers import is_billing_error  # type: ignore[import-not-found]
except ImportError:

    def is_billing_error(_exc: Exception) -> bool:  # noqa: N802
        """Stub when llm_providers is not installed."""
        return False


class ErrorCategory(enum.Enum):
    """Classification of execution errors for retry strategy selection."""

    TRANSIENT = "transient"
    BILLING = "billing"
    PERMANENT = "permanent"
    STALL = "stall"


_PERMANENT_PATTERNS: list[str] = [
    "policy denied",
    "permission denied",
    "not found",
    "unknown project",
]


def classify_error(exc: Exception) -> tuple[ErrorCategory, str]:
    """Classify an exception into a retry category with evidence.

    Returns (category, evidence) where evidence is a ≤125-char string
    describing why this classification was chosen.

    Categories (most-specific wins):
    - STALL: TimeoutError with 'stall' in message
    - TRANSIENT: TimeoutError (other) or generic errors
    - BILLING: matches billing/auth/rate-limit patterns
    - PERMANENT: FileNotFoundError, permission errors, policy denials
    """
    msg = str(exc).lower()
    raw = str(exc)

    if isinstance(exc, TimeoutError):
        if "stall" in msg:
            evidence = f"stall keyword in: {raw[:80]}"
            return ErrorCategory.STALL, evidence[:125]
        evidence = f"timeout: {raw[:100]}"
        return ErrorCategory.TRANSIENT, evidence[:125]

    if isinstance(exc, FileNotFoundError):
        evidence = f"file not found: {raw[:100]}"
        return ErrorCategory.PERMANENT, evidence[:125]

    if is_billing_error(raw):
        evidence = f"billing pattern matched: {raw[:100]}"
        return ErrorCategory.BILLING, evidence[:125]

    for pattern in _PERMANENT_PATTERNS:
        if pattern in msg:
            evidence = f"pattern '{pattern}' in: {raw[:80]}"
            return ErrorCategory.PERMANENT, evidence[:125]

    evidence = f"unclassified error: {raw[:100]}"
    return ErrorCategory.TRANSIENT, evidence[:125]


@dataclass(frozen=True)
class RetryStrategy:
    """Backoff strategy for a given error category."""

    max_retries: int
    base_delay_seconds: float
    max_backoff_seconds: float
    backoff_multiplier: float

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute delay in seconds for a given retry attempt (0-indexed).

        Uses exponential backoff capped at max_backoff_seconds.
        """
        raw = self.base_delay_seconds * (self.backoff_multiplier ** attempt)
        return min(raw, self.max_backoff_seconds)


_STRATEGIES: dict[ErrorCategory, RetryStrategy] = {
    ErrorCategory.TRANSIENT: RetryStrategy(
        max_retries=2, base_delay_seconds=10.0,
        max_backoff_seconds=300.0, backoff_multiplier=2.0,
    ),
    ErrorCategory.BILLING: RetryStrategy(
        max_retries=1, base_delay_seconds=60.0,
        max_backoff_seconds=60.0, backoff_multiplier=1.0,
    ),
    ErrorCategory.PERMANENT: RetryStrategy(
        max_retries=0, base_delay_seconds=0.0,
        max_backoff_seconds=0.0, backoff_multiplier=1.0,
    ),
    ErrorCategory.STALL: RetryStrategy(
        max_retries=2, base_delay_seconds=0.0,
        max_backoff_seconds=0.0, backoff_multiplier=1.0,
    ),
}


def get_strategy(category: ErrorCategory) -> RetryStrategy:
    """Return the retry strategy for a given error category."""
    return _STRATEGIES[category]
