"""Tests for TrustEngine — promotion, demotion, project-scoped trust."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ascend.trust_engine import TrustDecision, TrustEngine, TrustLevel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def trust(tmp_path: Path) -> TrustEngine:
    """Create a TrustEngine with a temp database."""
    return TrustEngine(tmp_path / "trust.db")


class TestRegistration:
    """Test agent registration and retrieval."""

    def test_auto_register_at_l0(self, trust: TrustEngine) -> None:
        level = trust.get_or_register_agent("new-agent")
        assert level == TrustLevel.L0

    def test_fast_lane_registration(self, trust: TrustEngine) -> None:
        trust.register_agent("fast-agent", fast_lane_level=TrustLevel.L2)
        assert trust.get_trust_level("fast-agent") == TrustLevel.L2

    def test_list_agents(self, trust: TrustEngine) -> None:
        trust.get_or_register_agent("a1")
        trust.get_or_register_agent("a2")
        agents = trust.list_agents()
        assert len(agents) == 2


class TestPromotion:
    """Test trust promotion after consecutive successes."""

    def test_promote_after_10_successes(self, trust: TrustEngine) -> None:
        trust.get_or_register_agent("worker")
        for i in range(10):
            trust.log_agent_run("worker", f"t{i}", "success", "")
        trust.evaluate_promotion("worker")
        assert trust.get_trust_level("worker") == TrustLevel.L1

    def test_no_promote_with_failure(self, trust: TrustEngine) -> None:
        trust.get_or_register_agent("worker")
        for i in range(9):
            trust.log_agent_run("worker", f"t{i}", "success", "")
        trust.log_agent_run("worker", "t9", "failure", "error")
        trust.evaluate_promotion("worker")
        assert trust.get_trust_level("worker") == TrustLevel.L0

    def test_no_promote_above_l4(self, trust: TrustEngine) -> None:
        trust.register_agent("elite", fast_lane_level=TrustLevel.L4)
        for i in range(10):
            trust.log_agent_run("elite", f"t{i}", "success", "")
        trust.evaluate_promotion("elite")
        assert trust.get_trust_level("elite") == TrustLevel.L4


class TestDemotion:
    """Test trust demotion after failures."""

    def test_demote_after_2_failures(self, trust: TrustEngine) -> None:
        trust.register_agent("risky", fast_lane_level=TrustLevel.L2)
        trust.log_agent_run("risky", "t1", "failure", "err1")
        trust.log_agent_run("risky", "t2", "failure", "err2")
        trust.evaluate_demotion("risky")
        assert trust.get_trust_level("risky") == TrustLevel.L1

    def test_no_demote_below_l0(self, trust: TrustEngine) -> None:
        trust.get_or_register_agent("bottom")
        trust.log_agent_run("bottom", "t1", "failure", "err")
        trust.log_agent_run("bottom", "t2", "failure", "err")
        trust.evaluate_demotion("bottom")
        assert trust.get_trust_level("bottom") == TrustLevel.L0


class TestDecision:
    """Test trust decision mapping."""

    def test_l0_read_only(self, trust: TrustEngine) -> None:
        trust.get_or_register_agent("l0-agent")
        assert trust.decide("l0-agent", "report") == TrustDecision.READ_ONLY

    def test_l1_requires_approval(self, trust: TrustEngine) -> None:
        trust.register_agent("l1-agent", fast_lane_level=TrustLevel.L1)
        assert trust.decide("l1-agent", "write") == TrustDecision.REQUIRES_APPROVAL

    def test_l2_auto_execute(self, trust: TrustEngine) -> None:
        trust.register_agent("l2-agent", fast_lane_level=TrustLevel.L2)
        assert trust.decide("l2-agent", "deploy") == TrustDecision.AUTO_EXECUTE
