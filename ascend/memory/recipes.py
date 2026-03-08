"""Workflow recipes — procedural memory for multi-step task execution.

Recipes are YAML files describing learned procedures that agents can follow.
Matched to tasks via keyword overlap in preconditions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RecipeStep:
    """A single step in a workflow recipe."""

    action: str
    tool: str = ""
    expected: str = ""


@dataclass
class Recipe:
    """A workflow recipe with preconditions and steps."""

    name: str
    domain: str
    discovered_by: str
    discovered_at: str
    confidence: float
    times_used: int
    success_rate: float
    preconditions: list[str]
    steps: list[RecipeStep]
    success_criteria: str = ""


class RecipeStore:
    """Load, match, and inject workflow recipes from data/procedures/."""

    def __init__(self, procedures_dir: Path) -> None:
        """Initialize with path to procedures directory.

        Args:
            procedures_dir: Path to data/procedures/ directory.
        """
        self._dir = procedures_dir
        self._recipes: list[Recipe] = []
        self._load_all()

    def _load_all(self) -> None:
        """Load all recipe YAML files from procedures directory."""
        if not self._dir.exists():
            return
        for yaml_file in self._dir.rglob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                recipe = self._parse_recipe(data)
                if recipe:
                    self._recipes.append(recipe)
            except (yaml.YAMLError, OSError) as exc:
                logger.warning("Failed to load recipe %s: %s", yaml_file, exc)
        if self._recipes:
            logger.info("Loaded %d workflow recipes", len(self._recipes))

    @staticmethod
    def _parse_recipe(data: dict[str, object]) -> Recipe | None:
        """Parse a recipe dict into a Recipe dataclass."""
        name = data.get("name")
        if not name or not isinstance(name, str):
            return None
        steps_raw = data.get("steps", [])
        if not isinstance(steps_raw, list):
            return None
        steps: list[RecipeStep] = []
        for step in steps_raw:
            if isinstance(step, dict):
                steps.append(RecipeStep(
                    action=str(step.get("action", "")),
                    tool=str(step.get("tool", "")),
                    expected=str(step.get("expected", "")),
                ))
        preconditions_raw = data.get("preconditions", [])
        preconditions = (
            [str(p) for p in preconditions_raw]
            if isinstance(preconditions_raw, list)
            else []
        )
        return Recipe(
            name=name,
            domain=str(data.get("domain", "")),
            discovered_by=str(data.get("discovered_by", "")),
            discovered_at=str(data.get("discovered_at", "")),
            confidence=float(str(data.get("confidence", 0.5))),
            times_used=int(str(data.get("times_used", 0))),
            success_rate=float(str(data.get("success_rate", 0.0))),
            preconditions=preconditions,
            steps=steps,
            success_criteria=str(data.get("success_criteria", "")),
        )

    def match(
        self,
        task_description: str,
        domain: str = "",
        min_overlap: float = 0.3,
    ) -> list[Recipe]:
        """Find recipes matching a task description via keyword overlap.

        Args:
            task_description: The task description to match against.
            domain: Optional domain filter.
            min_overlap: Minimum keyword overlap ratio (0-1).

        Returns:
            List of matching recipes, sorted by relevance.
        """
        task_words = set(task_description.lower().split())
        matches: list[tuple[float, Recipe]] = []

        for recipe in self._recipes:
            if domain and recipe.domain and recipe.domain != domain:
                continue
            # Compute overlap between task words and precondition words
            precondition_words: set[str] = set()
            for pc in recipe.preconditions:
                precondition_words.update(pc.lower().split())
            if not precondition_words:
                continue
            overlap = len(task_words & precondition_words) / len(precondition_words)
            if overlap >= min_overlap:
                score = overlap * recipe.confidence * recipe.success_rate
                matches.append((score, recipe))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in matches]

    def get_for_injection(
        self,
        task_description: str,
        domain: str = "",
        max_recipes: int = 2,
    ) -> str:
        """Get recipe context for injection into agent prompts.

        Args:
            task_description: Current task description.
            domain: Optional domain filter.
            max_recipes: Maximum recipes to inject.

        Returns:
            Formatted recipe context or empty string.
        """
        matched = self.match(task_description, domain)[:max_recipes]
        if not matched:
            return ""

        parts: list[str] = []
        for recipe in matched:
            steps_text = "\n".join(
                f"  {i+1}. {s.action}"
                + (f" (tool: {s.tool})" if s.tool else "")
                for i, s in enumerate(recipe.steps)
            )
            parts.append(
                f"[Recipe: {recipe.name}] "
                f"(confidence: {recipe.confidence:.1f}, "
                f"success rate: {recipe.success_rate:.0%})\n"
                f"Steps:\n{steps_text}"
            )

        return "## Relevant Workflow Recipes\n" + "\n\n".join(parts)

    @property
    def recipes(self) -> list[Recipe]:
        """Return loaded recipes."""
        return list(self._recipes)

    def record_usage(
        self,
        recipe_name: str,
        success: bool,
    ) -> None:
        """Record usage outcome for a recipe (updates in-memory only).

        Args:
            recipe_name: Name of the recipe used.
            success: Whether the recipe led to success.
        """
        for recipe in self._recipes:
            if recipe.name == recipe_name:
                total = recipe.times_used
                successes = round(recipe.success_rate * total)
                recipe.times_used = total + 1
                if success:
                    successes += 1
                recipe.success_rate = (
                    successes / recipe.times_used if recipe.times_used > 0
                    else 0.0
                )
                break
