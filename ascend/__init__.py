"""Ascend Core — trust-based agent orchestrator.

Provides executor, policy engine, trust engine, middleware pipeline,
memory architecture, and intelligence layer for building autonomous
agent systems with safety guarantees.
"""

from ascend.contracts import AgentResult, TaskContract, WorkflowContract, WorkflowResult
from ascend.policy_engine import PolicyEngine, PolicyResult
from ascend.trust_engine import TrustDecision, TrustEngine, TrustLevel

__all__ = [
    "AgentResult",
    "PolicyEngine",
    "PolicyResult",
    "TaskContract",
    "TrustDecision",
    "TrustEngine",
    "TrustLevel",
    "WorkflowContract",
    "WorkflowResult",
]

__version__ = "0.1.0"
