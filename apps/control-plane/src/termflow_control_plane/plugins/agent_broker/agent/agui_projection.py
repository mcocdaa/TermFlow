"""AG-UI 0.1.19 wire projection for canonical Agent events (plan M6a spec §4.3).

This module is the C-facing wire projection layer: it maps B's canonical
:class:`~termflow_control_plane.persistence.models.AgentEvent` kinds (plus the
bounded canonical payload JSON persisted on ``agent_events.payload``) to
AG-UI 0.1.19 wire event objects, hand-written against the pinned wire facts
(uppercase snake discriminators, camelCase field names, millisecond
``timestamp``) and pinned byte-exactly by the local fixture
``tests/fixtures/agui/agui-0.1.19-wire.json``.

Projection is **stateless and per-event**: a B event projects to at most one
AG-UI event (message text streams use the ``TEXT_MESSAGE_CHUNK`` convenience
event so a paginated REST replay never needs cross-event state to rebuild
START/CONTENT/END triples).  Unprojectable events are explicitly dropped with
a diagnostic counter (:class:`ProjectionDropCounts`) instead of inventing
AG-UI shapes; B's canonical events, opaque cursors, auth, retention, and
approval semantics remain the source of truth and are never leaked into the
AG-UI event objects (spec §4.3 "不泄露不变量").

``MAX_AGENT_EVENT_PAYLOAD_BYTES`` is the bounded-payload storage limit that
:class:`~termflow_control_plane.persistence.repositories.AgentEventRepository`
enforces on ``append(payload_json=...)`` (spec §4.2).
"""

from __future__ import annotations

#: Bounded canonical payload storage limit (spec §4.2; aligned with
#: ``MAX_CONTEXT_BYTES``).  Enforced by ``AgentEventRepository.append``.
MAX_AGENT_EVENT_PAYLOAD_BYTES = 64 * 1024
#: The pinned AG-UI protocol version this projection targets (spec §3).
AGUI_PROTOCOL_VERSION = "0.1.19"
#: RFC 6902 patch path used by ``STATE_DELTA`` (spec §4.3).
AGUI_STATE_PATH = "/backend"
#: ``CUSTOM`` event name for permission-request visibility (spec §4.3).
AGUI_PERMISSION_CUSTOM = "termflow.permission_requested"
