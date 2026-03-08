"""Skill eval framework — run assertion-based evals against SKILL.md agents."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

logger = logging.getLogger(__name__)

_EVALS_ROOT = Path(__file__).parent.parent / "evals"
_DATA_EVALS_ROOT = Path(__file__).parent.parent / "data" / "evals"


@dataclass
class EvalAssertion:
    """A single assertion to grade against agent output."""

    type: str  # "contains" | "not_contains" | "regex" | "min_length" | "json_valid"
    value: str
    description: str


@dataclass
class EvalCase:
    """A single eval test case with prompt and assertions."""

    id: str
    prompt: str
    assertions: list[EvalAssertion]
    baseline: str = ""  # empty = use SKILL.md prompt


@dataclass
class EvalResult:
    """Result of running a single eval case."""

    case_id: str
    skill: str
    passed: bool
    assertions_passed: list[str]
    assertions_failed: list[str]
    output: str
    duration_seconds: float
    tokens_used: int = 0


@dataclass
class EvalSuiteResult:
    """Result of running all cases for a skill."""

    skill: str
    iteration: int
    timestamp: str
    cases: list[EvalResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Fraction of cases that passed."""
        if not self.cases:
            return 0.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    @property
    def failed_cases(self) -> list[EvalResult]:
        """Cases that did not pass."""
        return [c for c in self.cases if not c.passed]


@dataclass
class EvalComparison:
    """Blind A/B comparison: same prompt, with vs without skill context."""

    case_id: str
    skill: str
    with_skill: EvalResult
    baseline: EvalResult
    winner: str  # "with_skill" | "baseline" | "tie"


class SkillEvaluator:
    """Run assertion-based evals for SKILL.md agents via executor."""

    def __init__(self, executor: object, evals_dir: Path | None = None) -> None:
        """Initialize with an AgentExecutor and optional evals directory.

        Args:
            executor: AgentExecutor instance (typed as object to avoid circular import).
            evals_dir: Root directory for evals JSON files. Defaults to evals/.
        """
        self._executor = executor
        self._evals_dir = evals_dir or _EVALS_ROOT

    def load_evals(self, skill: str) -> list[EvalCase]:
        """Load eval cases from evals/{skill}/evals.json.

        Args:
            skill: Skill name (e.g. "morning-briefing").

        Returns:
            List of EvalCase. Empty list if file not found.
        """
        path = self._evals_dir / skill / "evals.json"
        if not path.exists():
            logger.warning("No evals found for skill %r at %s", skill, path)
            return []
        data = json.loads(path.read_text())
        cases: list[EvalCase] = []
        for raw in data.get("cases", []):
            assertions = [
                EvalAssertion(
                    type=a["type"],
                    value=a["value"],
                    description=a["description"],
                )
                for a in raw.get("assertions", [])
            ]
            cases.append(EvalCase(
                id=raw["id"],
                prompt=raw["prompt"],
                assertions=assertions,
                baseline=raw.get("baseline", ""),
            ))
        return cases

    def run_case(self, case: EvalCase, skill: str) -> EvalResult:
        """Run a single eval case and grade the output.

        Builds a minimal TaskContract and calls executor.execute() synchronously.

        Args:
            case: The eval case to run.
            skill: Skill name for audit/tagging.

        Returns:
            EvalResult with pass/fail per assertion.
        """
        from ascend.contracts import TaskContract

        task = TaskContract(
            project="ascend",
            action="eval",
            description=case.prompt,
            target_files=[],
            trust_level=0,
            agent_type=skill,
            timeout_seconds=120,
            metadata={"eval_case_id": case.id, "eval_skill": skill},
        )

        start = time.monotonic()
        output = ""
        tokens_used = 0
        try:
            # executor.execute() is synchronous; eval runner calls it directly
            result = self._executor.execute(task)  # type: ignore[attr-defined]
            output = result.output if result else ""
            tokens_used = getattr(result, "tokens_used", 0) if result else 0
        except Exception as exc:
            logger.error("Eval case %r failed execution: %s", case.id, exc)
            output = ""
        duration = time.monotonic() - start

        passed_list, failed_list = self.grade_output(output, case.assertions)
        return EvalResult(
            case_id=case.id,
            skill=skill,
            passed=len(failed_list) == 0 and len(passed_list) > 0,
            assertions_passed=passed_list,
            assertions_failed=failed_list,
            output=output,
            duration_seconds=round(duration, 2),
            tokens_used=tokens_used,
        )

    def run_suite(self, skill: str) -> EvalSuiteResult:
        """Run all eval cases for a skill and persist results.

        Args:
            skill: Skill name.

        Returns:
            EvalSuiteResult with all case results and iteration number.
        """
        from datetime import datetime

        cases = self.load_evals(skill)
        iteration = self._next_iteration(skill)
        timestamp = datetime.now(UTC).isoformat()

        results: list[EvalResult] = []
        for case in cases:
            logger.info("Running eval case %r for skill %r", case.id, skill)
            result = self.run_case(case, skill)
            results.append(result)

        suite = EvalSuiteResult(
            skill=skill,
            iteration=iteration,
            timestamp=timestamp,
            cases=results,
        )
        self._persist_results(suite)
        return suite

    def grade_output(
        self, output: str, assertions: list[EvalAssertion],
    ) -> tuple[list[str], list[str]]:
        """Grade output against assertions.

        Args:
            output: Raw agent output string.
            assertions: List of EvalAssertion.

        Returns:
            Tuple of (passed_descriptions, failed_descriptions).
        """
        passed: list[str] = []
        failed: list[str] = []

        for assertion in assertions:
            ok = self._check_assertion(output, assertion)
            if ok:
                passed.append(assertion.description)
            else:
                failed.append(assertion.description)

        return passed, failed

    def _check_assertion(self, output: str, assertion: EvalAssertion) -> bool:
        """Check a single assertion against output."""
        atype = assertion.type
        value = assertion.value

        if atype == "contains":
            return value.lower() in output.lower()
        if atype == "not_contains":
            return value.lower() not in output.lower()
        if atype == "regex":
            return bool(re.search(value, output, re.IGNORECASE))
        if atype == "min_length":
            return len(output) >= int(value)
        if atype == "json_valid":
            try:
                json.loads(output)
                return True
            except (json.JSONDecodeError, ValueError):
                return False
        logger.warning("Unknown assertion type %r", atype)
        return False

    def critique_assertions(self, cases: list[EvalCase]) -> list[dict[str, str]]:
        """Flag assertions likely to pass on wrong output (too broad/trivial).

        Returns:
            List of {case_id, assertion, reason} dicts.
        """
        issues: list[dict[str, str]] = []
        for case in cases:
            for assertion in case.assertions:
                reason = self._critique_single(assertion)
                if reason:
                    issues.append({
                        "case_id": case.id,
                        "assertion": assertion.description,
                        "reason": reason,
                    })
        return issues

    def _critique_single(self, assertion: EvalAssertion) -> str:
        """Return non-empty string if assertion is weak, empty if OK."""
        if assertion.type == "min_length" and int(assertion.value) < 50:
            return f"min_length={assertion.value} too low — passes on near-empty output"
        if assertion.type == "contains":
            if len(assertion.value) < 4:
                return f"contains={assertion.value!r} too short — spurious matches likely"
            if assertion.value.lower() in ("error", "ok", "yes", "no", "true", "false"):
                return f"contains={assertion.value!r} matches in unexpected contexts"
        if assertion.type == "regex" and assertion.value in (".*", ".+", r"\w+", r"\w"):
            return f"regex={assertion.value!r} too permissive"
        return ""

    def run_comparison(self, case: EvalCase, skill: str) -> EvalComparison:
        """Run case with and without skill context, return blind comparison.

        Args:
            case: The eval case to run.
            skill: Skill name for audit/tagging.

        Returns:
            EvalComparison with winner: "with_skill" | "baseline" | "tie".
        """
        result_with = self.run_case(case, skill)
        baseline_case = EvalCase(
            id=case.id + "_baseline",
            prompt=case.prompt,
            assertions=case.assertions,
        )
        result_baseline = self.run_case(baseline_case, f"{skill}__baseline")
        if result_with.passed and not result_baseline.passed:
            winner = "with_skill"
        elif result_baseline.passed and not result_with.passed:
            winner = "baseline"
        else:
            winner = "tie"
        return EvalComparison(
            case_id=case.id,
            skill=skill,
            with_skill=result_with,
            baseline=result_baseline,
            winner=winner,
        )

    def aggregate_runs(self, skill: str, last_n: int = 5) -> dict[str, float]:
        """Mean±stddev for pass_rate, duration, tokens across last N iterations.

        Args:
            skill: Skill name.
            last_n: How many recent iterations to include.

        Returns:
            Dict with pass_rate_mean, pass_rate_stddev, duration_mean, tokens_mean.
            Empty dict if fewer than 2 iterations exist.
        """
        import statistics

        results_dir = _DATA_EVALS_ROOT / skill
        if not results_dir.exists():
            return {}
        iterations = sorted(results_dir.glob("iteration-*"), reverse=True)[:last_n]
        pass_rates: list[float] = []
        durations: list[float] = []
        tokens: list[int] = []
        for it_dir in iterations:
            path = it_dir / "results.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            pass_rates.append(float(str(data.get("pass_rate", 0))))
            for case in data.get("cases", []):
                durations.append(float(str(case.get("duration_seconds", 0))))
                tokens.append(int(str(case.get("tokens_used", 0))))
        if len(pass_rates) < 2:
            return {}
        return {
            "pass_rate_mean": round(statistics.mean(pass_rates), 4),
            "pass_rate_stddev": round(statistics.stdev(pass_rates), 4),
            "duration_mean": round(statistics.mean(durations), 2) if durations else 0.0,
            "tokens_mean": round(statistics.mean(tokens)) if tokens else 0,
        }

    def _next_iteration(self, skill: str) -> int:
        """Find the next iteration number for result storage."""
        skill_dir = _DATA_EVALS_ROOT / skill
        if not skill_dir.exists():
            return 1
        existing = sorted(skill_dir.glob("iteration-*"))
        if not existing:
            return 1
        last = existing[-1].name  # e.g. "iteration-3"
        try:
            return int(last.split("-")[1]) + 1
        except (IndexError, ValueError):
            return len(existing) + 1

    def _persist_results(self, suite: EvalSuiteResult) -> None:
        """Save EvalSuiteResult to data/evals/{skill}/iteration-N/results.json."""
        out_dir = _DATA_EVALS_ROOT / suite.skill / f"iteration-{suite.iteration}"
        out_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "skill": suite.skill,
            "iteration": suite.iteration,
            "timestamp": suite.timestamp,
            "pass_rate": round(suite.pass_rate, 4),
            "cases": [
                {
                    "case_id": r.case_id,
                    "passed": r.passed,
                    "assertions_passed": r.assertions_passed,
                    "assertions_failed": r.assertions_failed,
                    "duration_seconds": r.duration_seconds,
                    "tokens_used": r.tokens_used,
                }
                for r in suite.cases
            ],
        }
        out_path = out_dir / "results.json"
        out_path.write_text(json.dumps(payload, indent=2))
        logger.info(
            "Eval suite %r iteration %d: pass_rate=%.0f%% saved to %s",
            suite.skill, suite.iteration, suite.pass_rate * 100, out_path,
        )
