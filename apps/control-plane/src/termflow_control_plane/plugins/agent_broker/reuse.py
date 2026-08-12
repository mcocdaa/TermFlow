"""Executable record of the Agent Broker §22 reuse decisions (0.2.0).

Encodes the §22 reuse audit and adoption decisions table, the §22.1 reuse
invariants (authority, port-hiding, no provider types in protocol/domain,
and the direct-adoption contract), and the §22.2 implementation-order
guidance as a typed, machine-checkable contract:

- :data:`REUSE_DECISIONS` is the decision table, one entry per §22 candidate.
- :func:`reuse_invariants` returns the §22.1 rules as a static list.
- :class:`ReuseDecision` construction enforces the port-hiding invariant:
  adopt-like decisions must declare a ``required_port``, non-adopting
  decisions must not, and direct adoptions must carry a pinned version,
  license, and contract fixture.

The governing rule (plan §22): adopt a library when it removes a
protocol/parser/transport burden without taking ownership of TermFlow's
authority, persistence, or recovery semantics. Use an adapter/projection
when an external protocol is useful but its IDs, retention, or security
model do not match B. Do not add infrastructure merely because it already
implements a similar-sounding feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReuseDecisionKind(StrEnum):
    """The §22 decision taken for a candidate library or contract."""

    ADOPT_DIRECT = "adopt_direct"
    ADOPT_NARROWLY = "adopt_narrowly"
    EVALUATE = "evaluate"
    PARTIAL_REUSE = "partial_reuse"
    FUTURE_OPTION = "future_option"
    DO_NOT_USE = "do_not_use"


class TermFlowPort(StrEnum):
    """The §22.1 TermFlow ports a reused component may hide behind."""

    B_FEATURE_CONTEXT = "BFeatureContext"
    AGENT_BACKEND = "AgentBackend"
    BACKEND_TOOL_CONNECTOR = "BackendToolConnector"
    TERMINAL_OBSERVATION_PORT = "TerminalObservationPort"
    TERMINAL_COMMAND_PORT = "TerminalCommandPort"
    CONTINUATION_PORT = "ContinuationPort"
    TRANSCRIPTION_PROVIDER = "TranscriptionProvider"


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


@dataclass(frozen=True)
class ReuseDecision:
    """One §22 reuse decision for a candidate library or contract.

    Construction enforces the §22.1 invariants:

    - adopt-like decisions (ADOPT_DIRECT/ADOPT_NARROWLY/PARTIAL_REUSE/
      EVALUATE) must declare the ``required_port`` that hides the reused
      component from the domain;
    - non-adopting decisions (DO_NOT_USE/FUTURE_OPTION) must not declare a
      ``required_port``;
    - ADOPT_DIRECT additionally requires a pinned version, license, and
      contract fixture (§22.1 direct-adoption contract).
    """

    candidate: str
    decision: ReuseDecisionKind
    reuse_boundary: str
    required_port: TermFlowPort | None = None
    pinned_version: str | None = None
    license: str | None = None
    sbom_owner: str | None = None
    contract_fixture: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.decision in _ADOPT_LIKE_DECISIONS and self.required_port is None:
            raise ValueError(
                f"{self.candidate}: {self.decision.value} must hide behind a "
                "TermFlow port (required_port)"
            )
        if self.decision in _NON_ADOPT_DECISIONS and self.required_port is not None:
            raise ValueError(
                f"{self.candidate}: {self.decision.value} must not declare a "
                "required_port"
            )
        if self.decision is ReuseDecisionKind.ADOPT_DIRECT:
            if not (self.pinned_version or "").strip():
                raise ValueError(
                    f"{self.candidate}: ADOPT_DIRECT requires a pinned_version"
                )
            if not (self.license or "").strip():
                raise ValueError(f"{self.candidate}: ADOPT_DIRECT requires a license")
            if not (self.contract_fixture or "").strip():
                raise ValueError(
                    f"{self.candidate}: ADOPT_DIRECT requires a contract_fixture"
                )


REUSE_DECISIONS: tuple[ReuseDecision, ...] = (
    ReuseDecision(
        candidate="Official MCP Python SDK",
        decision=ReuseDecisionKind.ADOPT_DIRECT,
        reuse_boundary=(
            "B's MCP server/client transport, protocol negotiation, tool schemas, "
            "and Streamable HTTP"
        ),
        required_port=TermFlowPort.BACKEND_TOOL_CONNECTOR,
        pinned_version="mcp>=2.0,<3",
        license="MIT",
        sbom_owner="termflow-control-plane",
        contract_fixture=(
            "packages/protocol mcp.py; MCP handshake in "
            "tests/fixtures/opencode/opencode-pin.md"
        ),
        notes=(
            "Do not hand-write JSON-RPC/MCP framing; keep AgentToken, Origin/Host, "
            "scope/epoch, limits, and policy checks outside the SDK; MCP Streamable "
            "HTTP is never exposed to a browser; pinned at M0 commit 9d1aef8; mcp "
            "2.0.0 speaks two protocol eras on one streamable_http_app (legacy "
            "2025-11-25 initialize/session/Mcp-Session-Id and new 2026-07-28 "
            "no-session per-request _meta + server/discover); the negotiated era "
            "is verified against the pinned OpenCode image at the M4 gate; "
            "AgentToken verification implements the SDK TokenVerifier protocol "
            "(OAuth 2.1 resource server)."
        ),
    ),
    ReuseDecision(
        candidate="OpenCode Server/SDK contract",
        decision=ReuseDecisionKind.ADOPT_DIRECT,
        reuse_boundary="First AgentBackend implementation and HTTP/SSE decoder",
        required_port=TermFlowPort.AGENT_BACKEND,
        pinned_version=(
            "pinned /doc OpenAPI (tests/fixtures/opencode/opencode-openapi.json) "
            "and generated types; image digest pinned with the M4 image"
        ),
        license="Apache-2.0",
        sbom_owner="termflow-control-plane",
        contract_fixture="tests/fixtures/opencode/",
        notes=(
            "Pin /doc, generated types, image digest, and one SSE event mode "
            "(/global/event with GlobalEvent.directory); do not expose OpenCode "
            "types in the domain; ACP is out of the 0.2.0 HTTP/SSE compatibility "
            "scope."
        ),
    ),
    ReuseDecision(
        candidate="Pluggy",
        decision=ReuseDecisionKind.ADOPT_NARROWLY,
        reuse_boundary=(
            "In-process hook dispatch for the trusted B Feature Plugin registry"
        ),
        required_port=TermFlowPort.B_FEATURE_CONTEXT,
        license="MIT",
        sbom_owner="termflow-control-plane",
        notes=(
            "Wrapped by typed BFeatureContext and route/event/migration registries "
            "with dependency ordering and import-lint rules; Pluggy is not a "
            "sandbox, ACL layer, lifecycle manager, or migration runner; not yet "
            "added as a dependency."
        ),
    ),
    ReuseDecision(
        candidate="AG-UI",
        decision=ReuseDecisionKind.ADOPT_NARROWLY,
        reuse_boundary="C-facing projection of run/message/tool/state events",
        required_port=TermFlowPort.AGENT_BACKEND,
        pinned_version="ag-ui-protocol==0.1.19",
        license="MIT",
        sbom_owner="termflow-control-plane",
        contract_fixture=(
            "https://docs.ag-ui.com/api-reference/openapi.json "
            "(OpenAPI spec for ag-ui-protocol 0.1.19)"
        ),
        notes=(
            "Wire projection only; B keeps its own durable canonical event/cursor/"
            "auth model as the source of truth and AG-UI IDs/events are not the "
            "database contract; official Python SDK ag-ui-protocol 0.1.19 (MIT, "
            "pydantic>=2.11.2) with weekly spec releases; pre-1.0 watch item: "
            "THINKING-to-REASONING migration at 1.0.0."
        ),
    ),
    ReuseDecision(
        candidate="sse-starlette",
        decision=ReuseDecisionKind.ADOPT_NARROWLY,
        reuse_boundary="Optional B Agent SSE response implementation",
        required_port=TermFlowPort.AGENT_BACKEND,
        pinned_version=">=3.4,<4",
        license="BSD-3-Clause",
        sbom_owner="termflow-control-plane",
        notes=(
            "Optional SSE response implementation behind the AgentBackend event "
            "stream; subscribe-before-replay, opaque cursors, auth epoch closure, "
            "backpressure, and retention stay in B; already a direct dependency of "
            "mcp==2.0.0 (>=3.0.0) so the pin adds zero marginal dependency; "
            "disconnect detection and bounded channels confirmed."
        ),
    ),
    ReuseDecision(
        candidate="pyte",
        decision=ReuseDecisionKind.EVALUATE,
        reuse_boundary=(
            "Stateful VT/ANSI terminal rendering for Observation Service"
        ),
        required_port=TermFlowPort.TERMINAL_OBSERVATION_PORT,
        license="LGPL-3.0",
        sbom_owner="termflow-control-plane",
        notes=(
            "Can replace a custom screen-state parser but does not provide tmux "
            "stream cursors/gap/incarnation semantics; LGPL-3.0 license and "
            "escape-sequence behavior require a compatibility/security fixture "
            "before adoption; evaluate in M2 and retain the existing parser if "
            "license or parser fixtures fail."
        ),
    ),
    ReuseDecision(
        candidate="libtmux",
        decision=ReuseDecisionKind.PARTIAL_REUSE,
        reuse_boundary=(
            "Non-streaming tmux object traversal, provisioning, and isolated test "
            "fixtures"
        ),
        required_port=TermFlowPort.TERMINAL_COMMAND_PORT,
        license="MIT",
        sbom_owner="termflow-control-plane",
        notes=(
            "Typed Server/Session/Window/Pane operations and send/capture helpers "
            "for tests and provisioning only; never replaces the BridgeRuntime "
            "control-mode stream, bounded ring, or A/B protocol."
        ),
    ),
    ReuseDecision(
        candidate="OpenAI Whisper / faster-whisper",
        decision=ReuseDecisionKind.ADOPT_NARROWLY,
        reuse_boundary=(
            "Optional STT provider implementation behind TranscriptionProvider"
        ),
        required_port=TermFlowPort.TRANSCRIPTION_PROVIDER,
        license="MIT",
        sbom_owner="termflow-control-plane",
        notes=(
            "STT provider plugin only, not B logic; B owns upload limits, raw-audio "
            "deletion, draft confirmation, provider disclosure, and no-auto-submit; "
            "faster-whisper 1.2.1 (MIT) is the CTranslate2 implementation for a "
            "pinned CPU/GPU container but is in a maintenance lull; reference "
            "container is speaches-ai/speaches (MIT, active) at "
            "ghcr.io/speaches-ai/speaches:latest-cpu / :latest-cuda — the former "
            "fedirz/faster-whisper-server image is retired; first optional plugin "
            "in M7."
        ),
    ),
    ReuseDecision(
        candidate="Temporal / Restate",
        decision=ReuseDecisionKind.FUTURE_OPTION,
        reuse_boundary=(
            "Durable workflows, timers, signals, retries, and crash recovery"
        ),
        notes=(
            "0.3+ option, not a 0.2 default; could replace much of the custom "
            "Watch/Inbox scheduler but requires a workflow service/worker "
            "deployment and changes the SQLite single-worker model."
        ),
    ),
    ReuseDecision(
        candidate="NATS JetStream / Redis Streams",
        decision=ReuseDecisionKind.FUTURE_OPTION,
        reuse_boundary=(
            "Durable event transport, replay, consumer groups, and multi-worker "
            "delivery"
        ),
        notes=(
            "Future scale-out option; must not replace B's canonical authorization/"
            "timeline transaction in 0.2."
        ),
    ),
    ReuseDecision(
        candidate="A2A / ACP",
        decision=ReuseDecisionKind.DO_NOT_USE,
        reuse_boundary="Future remote-agent or agent-client interoperability",
        notes=(
            "Not the 0.2 OpenCode transport; ACP is out of scope per "
            "tests/fixtures/opencode/opencode-pin.md (stdio/bridge only); add only "
            "after a transport and capability fixture exists."
        ),
    ),
)


def reuse_invariants() -> list[str]:
    """Return the §22.1 reuse invariants as a static list of rules."""
    return [
        (
            "Authority: a reused protocol/library may parse, transport, schedule, "
            "or render data; it does not become the authority for Term ACL, "
            "AgentToken, approval, pane incarnation, B cursor, deletion, or "
            "retention."
        ),
        (
            "Port hiding: every reused component is hidden behind a TermFlow port "
            "(BFeatureContext, AgentBackend, BackendToolConnector, "
            "TerminalObservationPort, TerminalCommandPort, ContinuationPort, or "
            "TranscriptionProvider)."
        ),
        (
            "No provider types in protocol/domain: provider-only types, MCP parts, "
            "AG-UI events, tmux wrapper objects, and STT model objects do not enter "
            "packages/protocol or the canonical B/C domain models."
        ),
        (
            "Direct adoption contract: direct adoption requires pinned "
            "version/license/SBOM checks, a contract fixture, bounded payload "
            "tests, and a failure/reconnect test; popularity is not a compatibility "
            "or security argument."
        ),
    ]
