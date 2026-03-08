"""Policy engine — loads YAML configs and validates agent actions."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

if TYPE_CHECKING:
    from ascend.contracts import TaskContract

# Internal type alias for parsed YAML config sections.
_CfgMap = dict[str, object]


@dataclass(frozen=True)
class PolicyResult:
    """Outcome of a policy validation check."""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    requires_human: bool = False


class PolicyEngine:
    """Loads YAML policies and validates agent actions against them."""

    def __init__(self, policies_path: str, safety_path: str) -> None:
        """Load and parse both YAML config files."""
        self._policies_path = Path(policies_path)
        self._safety_path = Path(safety_path)
        self._policies: _CfgMap = {}
        self._safety: _CfgMap = {}
        self.reload()

    def reload(self) -> None:
        """Re-read YAML files from disk. For hot-reload without restart."""
        with open(self._policies_path) as f:
            self._policies = cast("_CfgMap", yaml.safe_load(f))
        with open(self._safety_path) as f:
            self._safety = cast("_CfgMap", yaml.safe_load(f))

    def validate_task(self, task: TaskContract) -> PolicyResult:
        """Full validation: action + blast radius + file paths + limits.

        Returns PolicyResult with approved/denied + reasons.
        """
        reasons: list[str] = []

        projects = self._get_projects()
        if task.project not in projects:
            return PolicyResult(
                approved=False,
                reasons=[f"unknown project: {task.project}"],
            )

        if not self.is_action_allowed(task.project, task.action):
            reasons.append(
                f"action '{task.action}' not allowed for "
                f"project '{task.project}'"
            )

        forbidden_hits = self.check_forbidden_paths(
            task.project, task.target_files
        )
        if forbidden_hits:
            reasons.append(
                f"forbidden paths matched: {forbidden_hits}"
            )

        project_cfg = projects[task.project]
        max_files = cast("int", project_cfg.get("max_files_per_task", 999))
        if len(task.target_files) > max_files:
            reasons.append(
                f"target_files count {len(task.target_files)} exceeds "
                f"max {max_files} for project '{task.project}'"
            )

        if reasons:
            return PolicyResult(approved=False, reasons=reasons)

        can_auto = self.check_blast_radius(task.project, task.trust_level)
        if not can_auto:
            return PolicyResult(
                approved=True,
                requires_human=True,
                reasons=["trust level too low for auto-approve"],
            )

        return PolicyResult(approved=True)

    def is_action_allowed(self, project: str, action: str) -> bool:
        """Check if action is in the project's allowed_actions list."""
        project_cfg = self._get_project_cfg(project)
        allowed = cast("list[str]", project_cfg.get("allowed_actions", []))
        return action in allowed

    def check_blast_radius(self, project: str, trust_level: int) -> bool:
        """Can this trust level auto-approve for this project's blast radius?"""
        project_cfg = self._get_project_cfg(project)
        radius = cast("str", project_cfg.get("blast_radius", "high"))
        levels = self._get_blast_radius_levels()
        level_cfg = levels.get(radius, {})
        min_trust = cast("int", level_cfg.get("auto_approve_min_trust", 4))
        return trust_level >= min_trust

    def check_forbidden_paths(
        self, project: str, target_files: list[str],
    ) -> list[str]:
        """Return list of target_files that match forbidden_paths globs."""
        project_cfg = self._get_project_cfg(project)
        forbidden = cast("list[str]", project_cfg.get("forbidden_paths", []))
        hits: list[str] = []
        for filepath in target_files:
            for pattern in forbidden:
                if fnmatch.fnmatch(filepath, pattern):
                    hits.append(filepath)
                    break
        return hits

    def get_timeout(self, requested: int) -> int:
        """Clamp requested timeout between default and max from safety.yaml."""
        timeouts = cast("_CfgMap", self._safety.get("timeouts", {}))
        default = cast("int", timeouts.get("default_task_seconds", 300))
        maximum = cast("int", timeouts.get("max_task_seconds", 900))
        if requested <= 0:
            return default
        return min(requested, maximum)

    def get_project_config(self, project: str) -> _CfgMap:
        """Return raw project config dict. Raises KeyError if unknown."""
        return self._get_project_cfg(project)

    def validate_agent_action(
        self, agent_type: str, action: str,
    ) -> PolicyResult:
        """Check if an agent type is allowed to perform an action.

        Uses the agents: section in policies.yaml.
        Returns PolicyResult with approved/denied + reasons.
        """
        agents = self._get_agents()
        if agent_type not in agents:
            return PolicyResult(
                approved=False,
                reasons=[f"unknown agent type: {agent_type}"],
            )

        agent_cfg = agents[agent_type]
        forbidden = cast("list[str]", agent_cfg.get("forbidden", []))
        allowed = cast("list[str]", agent_cfg.get("allowed", []))

        if action in forbidden:
            return PolicyResult(
                approved=False,
                reasons=[
                    f"action '{action}' is forbidden for "
                    f"agent '{agent_type}'"
                ],
            )

        if allowed and action not in allowed:
            return PolicyResult(
                approved=False,
                reasons=[
                    f"action '{action}' not in allowed list for "
                    f"agent '{agent_type}'"
                ],
            )

        return PolicyResult(approved=True)

    def has_agent_type(self, agent_type: str) -> bool:
        """Check if an agent type exists in the agents: section of policies."""
        return agent_type in self._get_agents()

    def get_agent_config(self, agent_type: str) -> _CfgMap:
        """Return raw agent config dict. Raises KeyError if unknown."""
        agents = self._get_agents()
        if agent_type not in agents:
            raise KeyError(f"unknown agent type: {agent_type}")
        return agents[agent_type]

    def _get_agents(self) -> dict[str, _CfgMap]:
        """Return the agents sub-dict from policies config."""
        return cast(
            "dict[str, _CfgMap]", self._policies.get("agents", {}),
        )

    def _get_projects(self) -> dict[str, _CfgMap]:
        """Return the projects sub-dict from policies config."""
        return cast(
            "dict[str, _CfgMap]", self._policies.get("projects", {}),
        )

    def _get_blast_radius_levels(self) -> dict[str, _CfgMap]:
        """Return the blast_radius_levels sub-dict from policies config."""
        return cast(
            "dict[str, _CfgMap]",
            self._policies.get("blast_radius_levels", {}),
        )

    def _get_project_cfg(self, project: str) -> _CfgMap:
        """Internal helper to fetch project config or raise KeyError."""
        projects = self._get_projects()
        if project not in projects:
            raise KeyError(f"unknown project: {project}")
        return projects[project]
