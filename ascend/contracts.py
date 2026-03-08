"""Core data contracts for the Ascend agent daemon."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

VALID_STATUSES = frozenset({"success", "failure", "timeout", "rejected"})
VALID_TRUST_LEVELS = range(0, 5)  # 0–4
VALID_WORKFLOW_STATUSES = frozenset({"success", "partial", "failure"})
VALID_ON_FAILURE = frozenset({"stop", "continue"})


def _utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _new_task_id() -> str:
    """Generate a new UUID4 task ID."""
    return uuid.uuid4().hex


def _new_workflow_id() -> str:
    """Generate a new UUID4 workflow ID."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class TaskContract:
    """Immutable contract describing a task for an agent to execute."""

    project: str
    action: str
    description: str
    target_files: list[str]
    trust_level: int
    task_id: str = field(default_factory=_new_task_id)
    created_at: str = field(default_factory=_utc_now)
    timeout_seconds: int = 300
    agent_type: str = ""
    agent_cli: str = ""
    max_turns: int = 0
    metadata: dict[str, str] = field(default_factory=dict)
    constraints: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate fields after creation."""
        if not self.project:
            raise ValueError("project must not be empty")
        if not self.action:
            raise ValueError("action must not be empty")
        if not self.description:
            raise ValueError("description must not be empty")
        if self.trust_level not in VALID_TRUST_LEVELS:
            raise ValueError(
                f"trust_level must be 0–4, got {self.trust_level}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be positive, got {self.timeout_seconds}"
            )


@dataclass
class AgentResult:
    """Mutable result built up during agent execution."""

    task_id: str
    status: str = "failure"
    output: str = ""
    error: str = ""
    files_changed: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = ""
    retries: int = 0
    retry_delays: list[float] = field(default_factory=list)
    error_categories: list[str] = field(default_factory=list)
    error_evidences: list[str] = field(default_factory=list)
    trust_delta: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_used: int = 0
    model_used: str = ""
    engine: str = ""
    duration_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    context_tokens_used: int = 0

    def __post_init__(self) -> None:
        """Validate fields after creation."""
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        self._validate_status()

    def _validate_status(self) -> None:
        """Check status is one of the allowed values."""
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_STATUSES)}, "
                f"got '{self.status}'"
            )

    def complete(self, status: str, output: str, error: str = "") -> None:
        """Mark the result as finished with final status."""
        if status not in VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_STATUSES)}, "
                f"got '{status}'"
            )
        self.status = status
        self.output = output
        self.error = error
        self.finished_at = _utc_now()

    def record_retry(
        self, delay: float = 0.0, error_category: str = "",
        error_evidence: str = "",
    ) -> None:
        """Increment the retry counter with optional delay, category, and evidence."""
        self.retries += 1
        self.retry_delays.append(delay)
        if error_category:
            self.error_categories.append(error_category)
        if error_evidence:
            self.error_evidences.append(error_evidence)


@dataclass(frozen=True)
class WorkflowContract:
    """Immutable contract describing a sequential workflow of tasks."""

    steps: list[TaskContract]
    on_failure: str = "stop"
    workflow_id: str = field(default_factory=_new_workflow_id)
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """Validate fields after creation."""
        if not self.steps:
            raise ValueError("steps must not be empty")
        if self.on_failure not in VALID_ON_FAILURE:
            raise ValueError(
                f"on_failure must be one of {sorted(VALID_ON_FAILURE)}, "
                f"got '{self.on_failure}'"
            )


@dataclass
class WorkflowResult:
    """Mutable result built up during workflow execution."""

    workflow_id: str
    status: str = "failure"
    step_results: list[AgentResult] = field(default_factory=list)
    completed_steps: int = 0
    total_steps: int = 0

    def __post_init__(self) -> None:
        """Validate fields after creation."""
        if not self.workflow_id:
            raise ValueError("workflow_id must not be empty")
        if self.status not in VALID_WORKFLOW_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_WORKFLOW_STATUSES)}, "
                f"got '{self.status}'"
            )
