"""Pre-gate deterministic check runner.

Runs linting and tests BEFORE agent execution to save model attention
for things that require thinking. Results are injected into agent context.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ASCEND_ROOT = Path(__file__).parent.parent
_POLICIES_PATH = _ASCEND_ROOT / "config" / "policies.yaml"


@dataclass
class PreGateCheck:
    """Individual pre-gate check result."""

    name: str
    passed: bool
    output: str  # first 500 chars of output
    duration_seconds: float


@dataclass
class PreGateResult:
    """Result of deterministic pre-gate checks."""

    passed: bool
    checks: dict[str, PreGateCheck] = field(default_factory=dict)
    summary: str = ""
    duration_seconds: float = 0.0


# --- Built-in check definitions per project ---

_ASCEND_CHECKS: list[dict[str, Any]] = [
    {
        "name": "ruff",
        "cmd": ["ruff", "check", ".", "--select", "E,F", "--no-fix"],
    },
    {
        "name": "pytest",
        "cmd": ["pytest", "tests/", "-x", "-q", "--tb=no"],
    },
]


def _load_policies() -> dict[str, Any]:
    """Load project policies from config/policies.yaml."""
    if not _POLICIES_PATH.exists():
        return {}
    with _POLICIES_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _has_test_command(project: str) -> bool:
    """Check if a project has run_tests in its allowed actions."""
    policies = _load_policies()
    projects = policies.get("projects", {})
    project_cfg = projects.get(project, {})
    allowed = project_cfg.get("allowed_actions", [])
    return "run_tests" in allowed


def _run_single_check(
    name: str, cmd: list[str], cwd: str, timeout: int = 30
) -> PreGateCheck:
    """Run a single deterministic check and capture output."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        output = (result.stdout + result.stderr).strip()
        return PreGateCheck(
            name=name,
            passed=result.returncode == 0,
            output=output[:500],
            duration_seconds=round(elapsed, 2),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return PreGateCheck(
            name=name,
            passed=False,
            output=f"TIMEOUT after {timeout}s",
            duration_seconds=round(elapsed, 2),
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - start
        return PreGateCheck(
            name=name,
            passed=False,
            output=f"Command not found: {cmd[0]}",
            duration_seconds=round(elapsed, 2),
        )


def run_checks(project: str, cwd: str) -> PreGateResult:
    """Run all pre-gate checks for a project.

    Args:
        project: Project name (must match keys in policies.yaml).
        cwd: Working directory to run checks in.

    Returns:
        PreGateResult with pass/fail status and individual check results.
    """
    start = time.monotonic()
    checks: dict[str, PreGateCheck] = {}

    if project == "ascend":
        checks = _run_ascend_checks(cwd)
    elif _has_test_command(project):
        checks = _run_generic_checks(project, cwd)
    else:
        logger.info("No pre-gate checks configured for project %s", project)

    total_elapsed = round(time.monotonic() - start, 2)
    all_passed = all(c.passed for c in checks.values()) if checks else True
    summary = _build_summary(checks, total_elapsed)

    return PreGateResult(
        passed=all_passed,
        checks=checks,
        summary=summary,
        duration_seconds=total_elapsed,
    )


def _run_ascend_checks(cwd: str) -> dict[str, PreGateCheck]:
    """Run Ascend-specific checks (ruff + pytest)."""
    checks: dict[str, PreGateCheck] = {}
    for check_def in _ASCEND_CHECKS:
        result = _run_single_check(check_def["name"], check_def["cmd"], cwd)
        checks[check_def["name"]] = result
        logger.debug(
            "Pre-gate %s: %s (%.2fs)",
            check_def["name"],
            "PASS" if result.passed else "FAIL",
            result.duration_seconds,
        )
    return checks


def _run_generic_checks(project: str, cwd: str) -> dict[str, PreGateCheck]:
    """Run generic checks for non-ascend projects with test support."""
    logger.info(
        "Project %s has run_tests action; no built-in checks yet", project
    )
    return {}


def _build_summary(
    checks: dict[str, PreGateCheck], total_seconds: float
) -> str:
    """Build a 1-2 line summary string from check results."""
    if not checks:
        return "[PRE-GATE] No checks configured"

    parts: list[str] = []
    for check in checks.values():
        status = "PASS" if check.passed else "FAIL"
        parts.append(f"{check.name}: {status}")
    header = f"[PRE-GATE] {' | '.join(parts)} ({total_seconds}s)"
    return header


def format_for_context(result: PreGateResult) -> str:
    """Format pre-gate results for injection into agent context.

    Returns a short string suitable for prepending to agent prompts.
    Failures include truncated output details.

    Args:
        result: The PreGateResult to format.

    Returns:
        Formatted string ready for context injection.
    """
    if not result.checks:
        return result.summary

    lines: list[str] = [result.summary]
    failures = {
        name: c for name, c in result.checks.items() if not c.passed
    }
    if failures:
        lines.append("Details:")
        for name, check in failures.items():
            lines.append(f"  {name}: {check.output}")
    return "\n".join(lines)


class PreGateRunner:
    """Stateful pre-gate runner bound to a project and workspace.

    Usage:
        runner = PreGateRunner("ascend", "/path/to/ascend")
        result = runner.run()
        context_str = runner.format(result)
    """

    def __init__(self, project: str, workspace: str) -> None:
        """Initialize runner for a specific project.

        Args:
            project: Project name matching policies.yaml keys.
            workspace: Absolute path to the project workspace.
        """
        self.project = project
        self.workspace = workspace

    def run(self) -> PreGateResult:
        """Execute all pre-gate checks for this project."""
        return run_checks(self.project, self.workspace)

    def format(self, result: PreGateResult) -> str:
        """Format result for context injection."""
        return format_for_context(result)
