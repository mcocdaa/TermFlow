"""Executable contract tests for the Agent Broker §22 reuse decisions.

These tests freeze the §22 reuse audit and adoption decisions and the §22.1
reuse invariants as an executable record: every §22 candidate is present with
the correct decision, adopt-like decisions hide behind a TermFlow port,
non-adopting decisions declare none, and construction validation rejects
decisions that violate the port-hiding or direct-adoption rules.
"""

from __future__ import annotations

import pytest
from termflow_control_plane.plugins.agent_broker.reuse import (
    REUSE_DECISIONS,
    ReuseDecision,
    ReuseDecisionKind,
    TermFlowPort,
    reuse_invariants,
)

_ADOPT_LIKE_DECISIONS: frozenset[ReuseDecisionKind] = frozenset(
    {
        ReuseDecisionKind.ADOPT_DIRECT,
        ReuseDecisionKind.ADOPT_NARROWLY,
        ReuseDecisionKind.PARTIAL_REUSE,
        ReuseDecisionKind.EVALUATE,
    }
)

_NON_ADOPT_DECISIONS: frozenset[ReuseDecisionKind] = frozenset(
    {ReuseDecisionKind.DO_NOT_USE, ReuseDecisionKind.FUTURE_OPTION}
)

#: The §22 candidate table: candidate -> expected decision for 0.2.0.
EXPECTED_SECTION_22_DECISIONS: dict[str, ReuseDecisionKind] = {
    "official mcp python sdk": ReuseDecisionKind.ADOPT_DIRECT,
    "opencode server/sdk contract": ReuseDecisionKind.ADOPT_DIRECT,
    "pluggy": ReuseDecisionKind.ADOPT_NARROWLY,
    "ag-ui": ReuseDecisionKind.ADOPT_NARROWLY,
    "sse-starlette": ReuseDecisionKind.ADOPT_NARROWLY,
    "pyte": ReuseDecisionKind.EVALUATE,
    "libtmux": ReuseDecisionKind.PARTIAL_REUSE,
    "temporal / restate": ReuseDecisionKind.FUTURE_OPTION,
    "nats jetstream / redis streams": ReuseDecisionKind.FUTURE_OPTION,
    "a2a / acp": ReuseDecisionKind.DO_NOT_USE,
}


def _decision(candidate: str) -> ReuseDecision:
    needle = candidate.lower()
    for entry in REUSE_DECISIONS:
        if entry.candidate.lower() == needle:
            return entry
    raise AssertionError(f"no §22 reuse decision recorded for candidate {candidate!r}")


class TestSection22Decisions:
    def test_all_section_22_candidates_present_with_correct_decisions(self) -> None:
        assert len(REUSE_DECISIONS) == 10
        assert len({entry.candidate for entry in REUSE_DECISIONS}) == 10
        assert {entry.candidate.lower() for entry in REUSE_DECISIONS} == set(
            EXPECTED_SECTION_22_DECISIONS
        )
        for candidate, expected in EXPECTED_SECTION_22_DECISIONS.items():
            assert _decision(candidate).decision is expected

    def test_direct_adoptions_pin_version_license_fixture_and_port(self) -> None:
        direct = [
            entry
            for entry in REUSE_DECISIONS
            if entry.decision is ReuseDecisionKind.ADOPT_DIRECT
        ]
        assert direct, "expected at least one ADOPT_DIRECT entry"
        for entry in direct:
            assert entry.pinned_version
            assert entry.license
            assert entry.contract_fixture
            assert entry.required_port is not None

    def test_port_hiding_invariant_for_every_entry(self) -> None:
        for entry in REUSE_DECISIONS:
            if entry.decision in _ADOPT_LIKE_DECISIONS:
                assert entry.required_port in TermFlowPort
            else:
                assert entry.required_port is None

    def test_pluggy_is_narrow_adoption_not_a_sandbox_or_acl(self) -> None:
        pluggy = _decision("Pluggy")
        assert pluggy.decision is ReuseDecisionKind.ADOPT_NARROWLY
        assert pluggy.required_port is TermFlowPort.B_FEATURE_CONTEXT
        assert pluggy.pinned_version is None
        assert pluggy.notes is not None
        assert "sandbox" in pluggy.notes
        assert "ACL" in pluggy.notes

    def test_pyte_is_evaluate_behind_observation_port_with_lgpl_license(self) -> None:
        pyte = _decision("pyte")
        assert pyte.decision is ReuseDecisionKind.EVALUATE
        assert pyte.license == "LGPL-3.0"
        assert pyte.required_port is TermFlowPort.TERMINAL_OBSERVATION_PORT

    def test_ag_ui_adopted_narrowly_as_wire_projection(self) -> None:
        ag_ui = _decision("AG-UI")
        assert ag_ui.decision is ReuseDecisionKind.ADOPT_NARROWLY
        assert ag_ui.required_port is TermFlowPort.AGENT_BACKEND
        assert ag_ui.pinned_version == "ag-ui-protocol==0.1.19"
        assert ag_ui.license == "MIT"
        assert ag_ui.contract_fixture is not None
        assert "docs.ag-ui.com/api-reference/openapi.json" in ag_ui.contract_fixture
        assert ag_ui.notes is not None
        assert "source of truth" in ag_ui.notes
        assert "wire projection" in ag_ui.notes.lower()

    def test_sse_starlette_pin_is_direct_mcp_dependency(self) -> None:
        sse_starlette = _decision("sse-starlette")
        assert sse_starlette.decision is ReuseDecisionKind.ADOPT_NARROWLY
        assert sse_starlette.pinned_version == ">=3.4,<4"
        assert sse_starlette.license == "BSD-3-Clause"
        assert sse_starlette.notes is not None
        assert "mcp==2.0.0" in sse_starlette.notes
        assert "zero marginal dependency" in sse_starlette.notes

    def test_mcp_sdk_notes_dual_era_and_token_verifier(self) -> None:
        mcp_sdk = _decision("Official MCP Python SDK")
        assert mcp_sdk.notes is not None
        assert "2026-07-28" in mcp_sdk.notes
        assert "2025-11-25" in mcp_sdk.notes
        assert "TokenVerifier" in mcp_sdk.notes

    def test_a2a_acp_do_not_use(self) -> None:
        a2a_acp = _decision("A2A / ACP")
        assert a2a_acp.decision is ReuseDecisionKind.DO_NOT_USE
        assert a2a_acp.required_port is None

    def test_reuse_invariants_encode_authority_and_port_hiding(self) -> None:
        invariants = reuse_invariants()
        assert invariants
        joined = " ".join(invariants).lower()
        assert "authority" in joined
        assert "port" in joined


class TestReuseDecisionValidation:
    def test_rejects_direct_adoption_without_pinned_version(self) -> None:
        with pytest.raises(ValueError):
            ReuseDecision(
                candidate="Fake SDK",
                decision=ReuseDecisionKind.ADOPT_DIRECT,
                reuse_boundary="boundary",
                required_port=TermFlowPort.AGENT_BACKEND,
            )

    @pytest.mark.parametrize("kind", _ADOPT_LIKE_DECISIONS)
    def test_adopt_like_decisions_require_a_port(self, kind: ReuseDecisionKind) -> None:
        with pytest.raises(ValueError):
            ReuseDecision(candidate="Fake", decision=kind, reuse_boundary="boundary")

    @pytest.mark.parametrize("kind", _NON_ADOPT_DECISIONS)
    def test_non_adopt_decisions_reject_a_port(self, kind: ReuseDecisionKind) -> None:
        with pytest.raises(ValueError):
            ReuseDecision(
                candidate="Fake",
                decision=kind,
                reuse_boundary="boundary",
                required_port=TermFlowPort.AGENT_BACKEND,
            )

    def test_rejects_do_not_use_with_required_port(self) -> None:
        with pytest.raises(ValueError):
            ReuseDecision(
                candidate="Fake",
                decision=ReuseDecisionKind.DO_NOT_USE,
                reuse_boundary="boundary",
                required_port=TermFlowPort.AGENT_BACKEND,
            )
