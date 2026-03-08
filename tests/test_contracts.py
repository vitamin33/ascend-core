"""Tests for core contracts — TaskContract, AgentResult, WorkflowContract."""

from __future__ import annotations

import pytest

from ascend.contracts import AgentResult, TaskContract, WorkflowContract


class TestTaskContract:
    """Test TaskContract validation and defaults."""

    def test_valid_creation(self) -> None:
        task = TaskContract(
            project="test",
            action="report",
            description="Test task",
            target_files=[],
            trust_level=0,
        )
        assert task.project == "test"
        assert task.timeout_seconds == 300
        assert task.task_id  # auto-generated

    def test_empty_project_raises(self) -> None:
        with pytest.raises(ValueError, match="project must not be empty"):
            TaskContract(
                project="",
                action="report",
                description="Test",
                target_files=[],
                trust_level=0,
            )

    def test_invalid_trust_level_raises(self) -> None:
        with pytest.raises(ValueError, match="trust_level must be 0–4"):
            TaskContract(
                project="test",
                action="report",
                description="Test",
                target_files=[],
                trust_level=5,
            )

    def test_negative_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            TaskContract(
                project="test",
                action="report",
                description="Test",
                target_files=[],
                trust_level=0,
                timeout_seconds=-1,
            )


class TestAgentResult:
    """Test AgentResult completion and retry tracking."""

    def test_complete_success(self) -> None:
        result = AgentResult(task_id="t1")
        result.complete("success", output="done")
        assert result.status == "success"
        assert result.output == "done"
        assert result.finished_at  # timestamp set

    def test_invalid_status_raises(self) -> None:
        result = AgentResult(task_id="t1")
        with pytest.raises(ValueError, match="status must be one of"):
            result.complete("invalid", output="")

    def test_record_retry(self) -> None:
        result = AgentResult(task_id="t1")
        result.record_retry(delay=1.5, error_category="transient")
        assert result.retries == 1
        assert result.retry_delays == [1.5]
        assert result.error_categories == ["transient"]


class TestWorkflowContract:
    """Test WorkflowContract validation."""

    def test_valid_workflow(self) -> None:
        task = TaskContract(
            project="test",
            action="report",
            description="Step 1",
            target_files=[],
            trust_level=0,
        )
        wf = WorkflowContract(steps=[task])
        assert len(wf.steps) == 1

    def test_empty_steps_raises(self) -> None:
        with pytest.raises(ValueError, match="steps must not be empty"):
            WorkflowContract(steps=[])
