"""Secret injection layer — ensures credentials never appear in agent context.

Scans context text for known secret patterns and replaces them with
placeholders. Secrets are loaded from env vars and .secrets/ files at
startup and stored as compiled patterns for fast matching.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Common secret patterns (regex)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(
        r"(?:sk-[a-zA-Z0-9]{20,}|"  # OpenAI/Anthropic style
        r"AIza[a-zA-Z0-9_-]{35}|"  # Google API
        r"ghp_[a-zA-Z0-9]{36}|"  # GitHub PAT
        r"ghs_[a-zA-Z0-9]{36}|"  # GitHub App
        r"xoxb-[a-zA-Z0-9-]{50,}|"  # Slack bot
        r"xoxp-[a-zA-Z0-9-]{50,})"  # Slack user
    )),
    ("bearer_token", re.compile(
        r"Bearer\s+[a-zA-Z0-9._~+/=-]{20,}",
    )),
    ("basic_auth", re.compile(
        r"Basic\s+[a-zA-Z0-9+/=]{20,}",
    )),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
    )),
    ("connection_string", re.compile(
        r"(?:postgres|mysql|mongodb|redis)://\S+:\S+@\S+",
    )),
    ("aws_key", re.compile(
        r"AKIA[0-9A-Z]{16}",
    )),
]


class SecretGuard:
    """Strips secrets from context text before agent injection."""

    def __init__(
        self,
        secrets_dir: Path | None = None,
        env_prefixes: list[str] | None = None,
    ) -> None:
        """Initialize secret guard.

        Args:
            secrets_dir: Path to .secrets/ directory with credential files.
            env_prefixes: Env var prefixes to treat as secrets (e.g., ["ANTHROPIC_", "OPENAI_"]).
        """
        self._known_secrets: set[str] = set()
        self._patterns = list(_SECRET_PATTERNS)
        if secrets_dir:
            self._load_secrets_from_dir(secrets_dir)
        if env_prefixes:
            self._load_secrets_from_env(env_prefixes)

    def _load_secrets_from_dir(self, secrets_dir: Path) -> None:
        """Load secret values from .secrets/ files."""
        if not secrets_dir.exists():
            return
        for path in secrets_dir.iterdir():
            if path.is_file() and not path.name.startswith("."):
                try:
                    content = path.read_text().strip()
                    if content and len(content) >= 8:
                        self._known_secrets.add(content)
                except OSError:
                    continue
        if self._known_secrets:
            logger.info(
                "SecretGuard loaded %d secrets from %s",
                len(self._known_secrets), secrets_dir,
            )

    def _load_secrets_from_env(self, prefixes: list[str]) -> None:
        """Load secret values from environment variables."""
        for key, value in os.environ.items():
            if any(key.startswith(p) for p in prefixes) and len(value) >= 8:
                self._known_secrets.add(value)

    def scrub(self, text: str) -> str:
        """Remove all known secrets and patterns from text.

        Args:
            text: The context text to scrub.

        Returns:
            Text with secrets replaced by [REDACTED:{type}].
        """
        result = text

        # Replace known secret values
        for secret in self._known_secrets:
            if secret in result:
                result = result.replace(secret, "[REDACTED:secret]")

        # Replace pattern-matched secrets
        for name, pattern in self._patterns:
            result = pattern.sub(f"[REDACTED:{name}]", result)

        return result

    def has_secrets(self, text: str) -> bool:
        """Check if text contains any known secrets or patterns.

        Args:
            text: The text to check.

        Returns:
            True if any secrets detected.
        """
        for secret in self._known_secrets:
            if secret in text:
                return True
        return any(pattern.search(text) for _, pattern in self._patterns)

    @property
    def known_secret_count(self) -> int:
        """Return number of loaded known secrets."""
        return len(self._known_secrets)
