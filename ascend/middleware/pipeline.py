"""Injectable middleware pipeline for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ascend.middleware.budget_guard import BudgetGuard
    from ascend.middleware.loop_detection import LoopDetectionMiddleware
    from ascend.middleware.verification import PreCompletionVerifier


@dataclass
class MiddlewarePipeline:
    """Holds optional references to each middleware component.

    Injected into AgentExecutor at construction time.
    Each field is optional — executor checks before calling.
    """

    loop_detection: LoopDetectionMiddleware | None = field(default=None)
    context_builder: Any = field(default=None)  # User-provided context builder
    verifier: PreCompletionVerifier | None = field(default=None)
    budget_guard: BudgetGuard | None = field(default=None)
