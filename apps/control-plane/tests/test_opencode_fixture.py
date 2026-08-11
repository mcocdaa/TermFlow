"""Pinned OpenCode compatibility fixture tests (M0).

These tests freeze the compatibility baseline for the OpenCode reference
backend adapter (plan §6, §10, §6.1, §6.2.1):

- the pinned SSE event mode and the documented endpoint contract
  (``opencode-pin.md``),
- the frozen OpenCode config with deny-by-default permissions and no
  remote/default MCP, plugins, LSP, or downloaded tools
  (``opencode-config.yaml``),
- the captured OpenCode OpenAPI 3.1 spec (``opencode-openapi.json``); if the
  live spec could not be captured the gap is visible as an xfail, never a
  silent pass,
- the reference backend capability matrix (``capability_matrix.md``) where
  every unverified item is explicitly ``unknown``/``unsupported``,
- the ACP out-of-scope declaration.

The pin and capability matrix documents carry a single machine-readable JSON
block (```json <marker>``` fence) that is the parseable contract; the prose
around it is the human-readable contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opencode"

PIN_PATH = FIXTURE_DIR / "opencode-pin.md"
CONFIG_PATH = FIXTURE_DIR / "opencode-config.yaml"
OPENAPI_PATH = FIXTURE_DIR / "opencode-openapi.json"
MATRIX_PATH = FIXTURE_DIR / "capability_matrix.md"

#: The two SSE event modes listed in plan §6. Exactly one is pinned; the other
#: is recorded as the documented alternative.
EVENT_MODES = {"/global/event", "/event?directory="}

#: The exact TermFlow MCP tool allowlist (plan §10). The frozen config must
#: match this exactly - no more, no fewer.
TERMFLOW_TOOLS = {
    "termflow_list_panes",
    "termflow_pane_read",
    "termflow_pane_send_text",
    "termflow_pane_send_keys",
    "termflow_watch_create",
    "termflow_watch_list",
    "termflow_watch_get",
    "termflow_watch_cancel",
}

#: The documented endpoint contract from plan §6. The pin doc must record every
#: entry and the captured OpenAPI spec must contain every path.
ENDPOINT_CONTRACT = {
    "health": "GET /global/health",
    "session_create": "POST /session",
    "prompt_async": "POST /session/:id/prompt_async",
    "message": "GET /session/:id/message",
    "abort": "POST /session/:id/abort",
    "permissions": "POST /session/:id/permissions/:permissionID",
    "status": "GET /session/status",
    "session_get": "GET /session/:id",
    "session_delete": "DELETE /session/:id",
    "doc": "/doc",
}

#: Plan §6 API endpoints expressed as OpenAPI spec paths (path params use the
#: ``{sessionID}`` templating used by the captured spec). ``/doc`` is excluded:
#: it is the spec-serving endpoint (the pinned contract fixture), not an API
#: operation, so it does not appear in the spec's ``paths``.
ENDPOINT_SPEC_PATHS = {
    "health": "/global/health",
    "session_create": "/session",
    "prompt_async": "/session/{sessionID}/prompt_async",
    "message": "/session/{sessionID}/message",
    "abort": "/session/{sessionID}/abort",
    "permissions": "/session/{sessionID}/permissions/{permissionID}",
    "status": "/session/status",
    "session_get": "/session/{sessionID}",
    "session_delete": "/session/{sessionID}",
}

#: Capability matrix dimensions required by the plan (§6/§6.1/§6.2.1).
REQUIRED_MATRIX_DIMENSIONS = {
    "accepted_input_kinds",
    "event_dedup_identity",
    "submit_idempotency",
    "run_boundary_inference",
    "cancel",
    "resume_delete",
    "isolation",
}

#: Every matrix dimension must carry an explicit verification status; nothing
#: may silently claim "verified". Only "unknown"/"unsupported" are acceptable
#: until the live container contract tests (M4) pass.
VERIFICATION_STATUS_VOCABULARY = {"unknown", "unsupported"}

#: Values that would silently overstate a claim in the capability matrix.
FORBIDDEN_OPTIMISTIC_VALUES = {"yes", "y", "true", True}


def _extract_json_block(path: Path, marker: str) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"```json\s+{re.escape(marker)}\s*\n(.*?)```",
        text,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing ```json {marker}``` block in {path.name}")
    return json.loads(match.group(1))


def _pin_contract() -> dict:
    return _extract_json_block(PIN_PATH, "termflow-pin-contract")


def _matrix() -> dict:
    return _extract_json_block(MATRIX_PATH, "termflow-capability-matrix")


def _load_config() -> dict:
    """Load the frozen config fixture.

    The file is YAML with a JSON-formatted body; ``#`` comment lines carry the
    human-readable documentation. No external YAML dependency is needed.
    """
    body: list[str] = []
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        body.append(line)
    return json.loads("\n".join(body))


def _load_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _assert_no_silent_yes(value: object, path: str) -> None:
    """Recursively reject bare "yes"/``True`` claims in the matrix."""
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_silent_yes(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_silent_yes(child, f"{path}[{index}]")
    else:
        assert value not in FORBIDDEN_OPTIMISTIC_VALUES, (
            f"{path} uses the forbidden optimistic value {value!r}; "
            "mark unverified items explicitly unknown/unsupported"
        )


class TestFixtureFiles:
    def test_fixture_files_exist(self) -> None:
        for path in (PIN_PATH, CONFIG_PATH, OPENAPI_PATH, MATRIX_PATH):
            assert path.is_file(), f"missing fixture: {path}"


class TestPinDoc:
    def test_exactly_one_selected_event_mode(self) -> None:
        contract = _pin_contract()
        selected = contract["selected_event_mode"]
        alternative = contract["alternative_event_mode"]
        assert isinstance(selected, str) and selected in EVENT_MODES
        assert isinstance(alternative, str) and alternative in EVENT_MODES
        # Exactly one mode is selected; the other is only the alternative.
        assert {selected, alternative} == EVENT_MODES
        # The prose must agree with the machine-readable block.
        prose = PIN_PATH.read_text(encoding="utf-8")
        assert f"**Pinned SSE event mode:** `{selected}`" in prose
        assert f"**Alternative SSE event mode:** `{alternative}`" in prose

    def test_endpoint_contract_complete(self) -> None:
        contract = _pin_contract()
        endpoints = contract["endpoints"]
        assert set(endpoints) == set(ENDPOINT_CONTRACT)
        for name, expected in ENDPOINT_CONTRACT.items():
            recorded = endpoints[name]
            assert isinstance(recorded, str) and recorded == expected, (
                f"endpoint {name} mis-recorded: {recorded!r} != {expected!r}"
            )

    def test_directory_persistence_rule_present(self) -> None:
        contract = _pin_contract()
        rule = contract["directory_persistence_rule"]
        assert isinstance(rule, str) and len(rule) >= 20
        prose = PIN_PATH.read_text(encoding="utf-8")
        assert "directory" in prose.lower()

    def test_no_exactly_once_claim_note_present(self) -> None:
        contract = _pin_contract()
        note = contract["no_exactly_once"]
        assert isinstance(note, dict)
        assert note["prompt_async_status"] == "204"
        assert note["transport_idempotency_key"] == "none"
        assert "no silent duplicate action" in note["guarantee"].lower()
        prose = PIN_PATH.read_text(encoding="utf-8")
        assert "exactly-once" in prose

    def test_mcp_handshake_expectations_non_empty_and_consistent(self) -> None:
        contract = _pin_contract()
        handshake = contract["mcp_handshake"]
        for key in (
            "protocol_revision",
            "content_type",
            "accept",
            "host_allowed",
            "origin_allowed",
            "request_ids",
            "session_lifecycle",
            "notification_behavior",
        ):
            value = handshake[key]
            assert isinstance(value, str) and value.strip(), (
                f"mcp_handshake.{key} must be a non-empty string"
            )
        assert handshake["content_type"] == "application/json"
        assert "text/event-stream" in handshake["accept"]

    def test_acp_declaration_present(self) -> None:
        contract = _pin_contract()
        declaration = contract["acp_out_of_scope"]
        assert isinstance(declaration, str) and "out of scope" in declaration.lower()
        prose = PIN_PATH.read_text(encoding="utf-8")
        assert "ACP" in prose
        assert "out of scope" in prose.lower()
        assert "stdio" in prose


class TestConfig:
    def test_deny_default_and_exact_tool_allowlist(self) -> None:
        config = _load_config()
        permission = config["permission"]
        assert permission["*"] == "deny"
        configured_tools = set(permission) - {"*"}
        assert configured_tools == TERMFLOW_TOOLS
        for tool in TERMFLOW_TOOLS:
            assert permission[tool] == "ask", (
                f"reviewed policy for {tool} must be 'ask' (B-mediated approval)"
            )

    def test_no_remote_mcp_plugins_lsp_downloaded_tools(self) -> None:
        config = _load_config()
        mcp = config.get("mcp")
        assert mcp is None or mcp == {}
        for key in ("plugin", "plugins", "lsp", "lspServers"):
            assert key not in config, f"config must not contain {key!r}"

    def test_config_is_parseable_as_openapi_shaped_json(self) -> None:
        config = _load_config()
        assert config.get("$schema") == "https://opencode.ai/config.json"
        assert isinstance(config["permission"], dict)

    def test_config_documents_startup_drift_guard(self) -> None:
        """Startup must fail on tool inventory/config drift (§10, M0)."""
        text = CONFIG_PATH.read_text(encoding="utf-8")
        assert "drift" in text.lower()
        assert "fail" in text.lower()
        assert "tool inventory" in text.lower()
        # The TermFlow MCP server is injected at runtime by the supervisor with
        # the epoch-bound token; it must not be baked into this no-defaults
        # fixture as a static entry.
        assert "termflow" in text.lower()


class TestOpenApiSpec:
    def test_plan6_endpoints_present_in_spec(self) -> None:
        spec = _load_openapi()
        status = spec.get("status")
        if status == "not_captured":
            reason = spec.get("reason")
            fallback = spec.get("fallback")
            assert isinstance(reason, str) and reason
            assert isinstance(fallback, str) and fallback
            pytest.xfail(
                "live OpenCode /doc not captured — requires network/container; "
                f"reason: {reason}"
            )
        # A real OpenAPI spec was captured; assert it is a spec and covers §6.
        assert "openapi" in spec
        assert "paths" in spec
        paths = spec["paths"]
        for name, path in ENDPOINT_SPEC_PATHS.items():
            assert path in paths, f"spec is missing plan §6 endpoint {name} ({path})"
        methods = {
            "health": {"get"},
            "session_create": {"post"},
            "prompt_async": {"post"},
            "message": {"get"},
            "abort": {"post"},
            "permissions": {"post"},
            "status": {"get"},
            "session_get": {"get"},
            "session_delete": {"delete"},
        }
        for name, expected_methods in methods.items():
            actual = set(paths[ENDPOINT_SPEC_PATHS[name]])
            assert expected_methods <= actual, (
                f"spec {ENDPOINT_SPEC_PATHS[name]} is missing methods "
                f"{expected_methods - actual}"
            )

    def test_pin_endpoints_correspond_to_spec_paths(self) -> None:
        """Cross-check: every pinned §6 API endpoint maps to a spec path.

        ``/doc`` is recorded in the pin contract but is the spec-serving
        endpoint (the pinned contract fixture), not an API operation, so it is
        intentionally absent from the spec's ``paths``.
        """
        spec = _load_openapi()
        if spec.get("status") == "not_captured":
            pytest.xfail("live OpenCode /doc not captured — requires network/container")
        contract = _pin_contract()
        endpoints = contract["endpoints"]
        assert endpoints["doc"] == "/doc"
        for name, recorded in endpoints.items():
            if name == "doc":
                continue
            method, path_template = recorded.split(" ", 1)
            spec_path = ENDPOINT_SPEC_PATHS[name]
            assert spec_path in spec["paths"]
            assert method.lower() in spec["paths"][spec_path]


class TestCapabilityMatrix:
    def test_all_dimensions_present(self) -> None:
        matrix = _matrix()
        dimensions = matrix["dimensions"]
        assert set(dimensions) == REQUIRED_MATRIX_DIMENSIONS
        for name in REQUIRED_MATRIX_DIMENSIONS:
            assert isinstance(dimensions[name], dict) and dimensions[name], (
                f"dimension {name!r} must be a non-empty mapping"
            )

    def test_unverified_items_are_explicit(self) -> None:
        matrix = _matrix()
        dimensions = matrix["dimensions"]
        for name in REQUIRED_MATRIX_DIMENSIONS:
            dimension = dimensions[name]
            assert "verification_status" in dimension
            status = dimension["verification_status"]
            assert status in VERIFICATION_STATUS_VOCABULARY, (
                f"dimension {name!r} verification_status {status!r} is not "
                f"one of {sorted(VERIFICATION_STATUS_VOCABULARY)}; unverified "
                "items must be explicitly unknown/unsupported"
            )
            _assert_no_silent_yes(dimension, f"dimensions.{name}")

    def test_required_dimension_content(self) -> None:
        matrix = _matrix()
        dimensions = matrix["dimensions"]
        kinds = dimensions["accepted_input_kinds"]
        for kind in (
            "user_message",
            "watch_triggered",
            "permission_resolved",
            "timer_triggered",
            "system_notification",
        ):
            assert kind in kinds, f"accepted_input_kinds missing {kind!r}"
        dedup = dimensions["event_dedup_identity"]
        assert isinstance(dedup["identity_components"], list) and dedup["identity_components"]
        submit = dimensions["submit_idempotency"]
        assert submit["mode"] == "non_idempotent"
        assert submit["transport_idempotency_key"] == "none"
        cancel = dimensions["cancel"]
        assert "abort" in cancel["endpoint"]
        resume = dimensions["resume_delete"]
        assert "volume" in resume["context_mode"]
        isolation = dimensions["isolation"]
        assert isolation["runtime_isolation"] == "binding"
        assert isolation["one_active_run_per_binding"] == "contractual"
        assert isolation["epoch_bound_mcp_capability"] == "contractual"


class TestAcpScope:
    def test_acp_declared_out_of_scope_in_pin_doc(self) -> None:
        prose = PIN_PATH.read_text(encoding="utf-8")
        assert "ACP" in prose
        assert "out of scope" in prose.lower()
        assert "stdio" in prose
