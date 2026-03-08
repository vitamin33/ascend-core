"""Pre-completion verification middleware."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ascend.intelligence.skill_evaluator import EvalResult


@dataclass
class VerificationResult:
    """Result of a pre-completion verification check."""

    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


CHECKLISTS: dict[str, list[str]] = {
    "code": [
        "Tests pass for changed code",
        "No lint errors introduced",
        "Type hints present on all functions",
        "No hardcoded secrets",
    ],
    "content": [
        "Grammar and spelling checked",
        "Links are valid",
        "Formatting is consistent",
    ],
    "pr": [
        "PR description is clear",
        "All commits are relevant",
        "No unrelated changes included",
        "CI checks pass",
    ],
    "analysis": [
        "Data sources are cited",
        "Conclusions follow from evidence",
        "Edge cases considered",
    ],
    "self_dev": [
        "All plan items implemented",
        "Tests added for new functionality",
        "No regressions in existing tests",
        "Documentation updated if needed",
        "Spec requirements 100% covered",
    ],
}


class PreCompletionVerifier:
    """Generate verification checklists and parse results."""

    def inject_verification_prompt(
        self, task_type: str, output: str
    ) -> str:
        """Return a verification checklist prompt for the given task type.

        Args:
            task_type: One of "code", "content", "pr", "analysis".
            output: The agent output to verify.

        Returns:
            Prompt string with checklist items.
        """
        checks = CHECKLISTS.get(task_type, CHECKLISTS["code"])
        checklist = "\n".join(f"- [ ] {item}" for item in checks)
        return (
            f"Verify the following output before marking complete:\n\n"
            f"--- OUTPUT ---\n{output}\n--- END OUTPUT ---\n\n"
            f"Checklist ({task_type}):\n{checklist}\n\n"
            f"For each item, respond PASS or FAIL with a brief reason."
        )

    def parse_verification_result(
        self, response: str
    ) -> VerificationResult:
        """Parse a verification response into a structured result.

        Args:
            response: Free-text response with PASS/FAIL lines.

        Returns:
            VerificationResult with parsed checks and issues.
        """
        checks: dict[str, bool] = {}
        issues: list[str] = []

        for line in response.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            upper = line.upper()
            if "PASS" in upper:
                checks[line] = True
            elif "FAIL" in upper:
                checks[line] = False
                issues.append(line)

        passed = len(issues) == 0 and len(checks) > 0
        return VerificationResult(passed=passed, checks=checks, issues=issues)


class ComplianceStatus(Enum):
    """Status of a single spec requirement."""

    DONE = "DONE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


@dataclass
class SpecItem:
    """A single requirement extracted from a spec with its compliance status."""

    requirement: str
    status: ComplianceStatus
    evidence: str = ""


@dataclass
class SpecComplianceResult:
    """Structured result of a spec compliance check."""

    items: list[SpecItem] = field(default_factory=list)

    @property
    def coverage_percent(self) -> float:
        """Calculate the percentage of items marked DONE."""
        if not self.items:
            return 0.0
        done_count = sum(
            1 for item in self.items
            if item.status == ComplianceStatus.DONE
        )
        return (done_count / len(self.items)) * 100.0

    @property
    def missing_items(self) -> list[str]:
        """Return requirements that are not fully DONE."""
        return [
            item.requirement
            for item in self.items
            if item.status != ComplianceStatus.DONE
        ]


# Regex patterns for extracting requirements from spec text
_NUMBERED_RE = re.compile(r"^\s*\d+[\.\)]\s+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?!\[[ x]\]\s)(.+)$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ x]\]\s+(.+)$", re.MULTILINE)
_KEYWORD_RE = re.compile(
    r"^.*\b(must|should|shall)\b\s+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)


class SpecComplianceChecker:
    """Check agent output against a plan/spec for compliance."""

    def extract_requirements(self, plan: str) -> list[str]:
        """Extract actionable requirements from a plan or spec text.

        Finds numbered items, bullet points, checkbox items, and
        sentences containing 'must', 'should', or 'shall'.

        Args:
            plan: The plan/spec text to extract from.

        Returns:
            Deduplicated list of requirement strings.
        """
        requirements: list[str] = []
        seen: set[str] = set()

        for pattern in (_CHECKBOX_RE, _NUMBERED_RE, _BULLET_RE):
            for match in pattern.finditer(plan):
                text = match.group(1).strip()
                if text and text not in seen:
                    seen.add(text)
                    requirements.append(text)

        for match in _KEYWORD_RE.finditer(plan):
            full_line = match.group(0).strip()
            if full_line and full_line not in seen:
                seen.add(full_line)
                requirements.append(full_line)

        return requirements

    def build_compliance_prompt(self, plan: str, output: str) -> str:
        """Build a prompt asking the model to verify spec compliance.

        Args:
            plan: The original plan/spec text.
            output: The agent's implementation output.

        Returns:
            Formatted prompt string for compliance checking.
        """
        requirements = self.extract_requirements(plan)
        req_list = "\n".join(
            f"{i}. {req}" for i, req in enumerate(requirements, 1)
        )
        return (
            "Check the implementation output against each requirement.\n\n"
            f"--- SPEC ---\n{plan}\n--- END SPEC ---\n\n"
            f"--- OUTPUT ---\n{output}\n--- END OUTPUT ---\n\n"
            f"Requirements to verify:\n{req_list}\n\n"
            "For each requirement, respond with exactly one line:\n"
            "<number>. DONE|PARTIAL|MISSING - <evidence>\n"
        )

    def parse_compliance_result(
        self, response: str,
    ) -> SpecComplianceResult:
        """Parse the model's compliance check response.

        Args:
            response: Model response with DONE/PARTIAL/MISSING lines.

        Returns:
            Structured SpecComplianceResult.
        """
        items: list[SpecItem] = []
        line_re = re.compile(
            r"^\s*\d+\.\s*(DONE|PARTIAL|MISSING)\s*-\s*(.*)$",
            re.IGNORECASE,
        )

        for line in response.strip().splitlines():
            match = line_re.match(line.strip())
            if not match:
                continue
            status_str = match.group(1).upper()
            evidence = match.group(2).strip()
            status = ComplianceStatus(status_str)
            requirement = self._extract_requirement(line)
            items.append(SpecItem(
                requirement=requirement,
                status=status,
                evidence=evidence,
            ))

        return SpecComplianceResult(items=items)

    def _extract_requirement(self, line: str) -> str:
        """Extract a readable requirement label from a result line."""
        cleaned = re.sub(r"^\s*\d+\.\s*", "", line.strip())
        cleaned = re.sub(
            r"^(DONE|PARTIAL|MISSING)\s*-\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    def check_coverage(self, result: SpecComplianceResult) -> bool:
        """Check whether all spec items are fully covered.

        Args:
            result: The compliance result to evaluate.

        Returns:
            True only if every item has status DONE.
        """
        if not result.items:
            return False
        return all(
            item.status == ComplianceStatus.DONE
            for item in result.items
        )



@dataclass
class AssertionGradeResult:
    """Richer grading from assertion-based eval, replaces binary PASS/FAIL.

    Used by the eval runner and TraceAnalyzer. Does not replace
    VerificationResult which is still used by the self-dev pipeline.
    """

    passed: bool
    pass_rate: float  # 0.0 – 1.0
    passed_checks: list[str]
    failed_checks: list[str]
    quality_score: float  # 0–10, maps to outcome_signals.json

    @classmethod
    def from_eval_result(cls, result: EvalResult) -> AssertionGradeResult:
        """Build from an EvalResult produced by SkillEvaluator.

        Args:
            result: Completed EvalResult from skill_evaluator.

        Returns:
            AssertionGradeResult with derived pass_rate and quality_score.
        """
        total = len(result.assertions_passed) + len(result.assertions_failed)
        rate = len(result.assertions_passed) / total if total else 0.0
        score = round(rate * 10, 1)
        return cls(
            passed=result.passed,
            pass_rate=round(rate, 4),
            passed_checks=result.assertions_passed,
            failed_checks=result.assertions_failed,
            quality_score=score,
        )
