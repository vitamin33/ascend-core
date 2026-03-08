"""Agent executor — runs tasks through policy/trust/audit pipeline.

This is the core execution abstraction. It validates tasks against policy,
checks trust levels, runs the agent via subprocess, and logs results.

For production use, extend this class to add:
- tmux support for long-running agents (L2+)
- CLI routing (claude, gemini, cline)
- Stream monitoring and token tracking
- Stall detection and workspace isolation
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ascend.contracts import AgentResult, TaskContract

if TYPE_CHECKING:
    from ascend.policy_engine import PolicyEngine
    from ascend.trust_engine import TrustEngine

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 5


class AgentExecutor:
    """Runs agent tasks with policy/trust validation and audit logging.

    This is the minimal executor showing the core pipeline:
    1. Policy validation (is this action allowed?)
    2. Trust check (what execution mode?)
    3. Agent execution (subprocess)
    4. Audit logging (append-only JSONL)
    5. Trust evaluation (promote/demote based on results)
    """

    def __init__(
        self,
        trust: TrustEngine,
        policy: PolicyEngine,
        audit_log: Path | None = None,
    ) -> None:
        """Initialize executor with trust and policy engines.

        Args:
            trust: Trust engine for agent permission management.
            policy: Policy engine for action/path validation.
            audit_log: Path to append-only audit JSONL file.
        """
        self._trust = trust
        self._policy = policy
        self._audit_path = audit_log or Path("logs/audit.jsonl")

    def execute(self, task: TaskContract, agent_id: str) -> AgentResult:
        """Execute a task through the full pipeline.

        Args:
            task: The task contract to execute.
            agent_id: The agent identifier.

        Returns:
            AgentResult with status, output, and metadata.
        """
        result = AgentResult(task_id=task.task_id)
        start = time.monotonic()

        # 1. Audit: log task start
        self._audit_log("task_start", task, agent_id)

        # 2. Policy validation
        policy_result = self._policy.validate_task(task)
        if not policy_result.approved:
            result.complete(
                "rejected",
                output="",
                error=f"Policy denied: {'; '.join(policy_result.reasons)}",
            )
            self._audit_log("task_rejected", task, agent_id, result=result)
            return result

        # 3. Agent-level policy check
        if task.agent_type:
            agent_policy = self._policy.validate_agent_action(
                task.agent_type, task.action,
            )
            if not agent_policy.approved:
                result.complete(
                    "rejected",
                    output="",
                    error=f"Agent policy denied: {'; '.join(agent_policy.reasons)}",
                )
                self._audit_log("task_rejected", task, agent_id, result=result)
                return result

        # 4. Trust check
        self._trust.get_or_register_agent(agent_id, project=task.project)
        self._trust.decide(agent_id, task.action)

        # 5. Execute
        try:
            output = self._run_subprocess(task)
            result.complete("success", output=output)
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            result.complete(
                "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failure",
                output="",
                error=str(exc),
            )

        # 6. Record duration
        result.duration_seconds = round(time.monotonic() - start, 1)

        # 7. Log run and evaluate trust
        self._trust.log_agent_run(
            agent_id, task.task_id, result.status, result.error,
            project=task.project,
        )
        if result.status == "success":
            self._trust.evaluate_promotion(agent_id)
        else:
            self._trust.evaluate_demotion(agent_id)

        # 8. Audit: log task complete
        self._audit_log("task_complete", task, agent_id, result=result)

        return result

    def _run_subprocess(self, task: TaskContract) -> str:
        """Run the agent task as a subprocess.

        Override this method to customize execution (e.g., tmux for L2+,
        different CLI tools, workspace isolation).
        """
        cmd = [
            "claude", "-p", task.description,
            "--verbose", "--output-format", "stream-json",
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=task.timeout_seconds,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"Agent process failed (exit {proc.returncode}): "
                f"{proc.stderr[:500]}"
            )

        output = proc.stdout.strip()
        if not output:
            raise RuntimeError("Agent produced empty output")

        return output

    def _audit_log(
        self,
        event: str,
        task: TaskContract,
        agent_id: str,
        result: AgentResult | None = None,
    ) -> None:
        """Append an event to the audit JSONL log."""
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

        entry: dict[str, object] = {
            "event": event,
            "timestamp": datetime.now(UTC).isoformat(),
            "task_id": task.task_id,
            "agent_id": agent_id,
            "project": task.project,
            "action": task.action,
        }

        if result is not None:
            entry["status"] = result.status
            entry["error"] = result.error
            entry["duration_seconds"] = result.duration_seconds

        try:
            with open(self._audit_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.error("Failed to write audit log: %s", exc)
