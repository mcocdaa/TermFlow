# TermFlow 0.2.0 STT Scope Correction Design

**Date:** 2026-08-16
**Status:** Approved architecture; implementation pending
**Supersedes:** Agent Broker plan M7, `2026-08-13-m7a-stt-container-design.md`, and `2026-08-13-m7b-mobile-stt-ui-design.md`

## 1. Decision

TermFlow treats every user submission from C as text. How C produced that text—keyboard, paste, IME, accessibility input, or an optional speech-to-text facility—is outside the A/B protocol and B runtime.

The only supported boundary is:

```text
keyboard / paste / optional C-side STT
                  |
                  v
             ordinary text
                  |
                  v
       existing Agent message submit path
```

Therefore TermFlow 0.2.0 does not expose or deploy an STT subsystem. Audio must not cross the C-to-B boundary, and B must not distinguish speech-derived text from any other text.

## 2. Required invariants

1. C submits only ordinary text through the existing Agent composer/message API.
2. A, B, shared protocol packages, and generated B/C contracts contain no audio upload or STT-specific surface.
3. B has no transcription provider, STT capability flag, transcript-draft state machine, audio staging, STT configuration, or STT lifecycle work.
4. The deployment has no STT container, model volume, image pin, provider credential, health check, or STT verification gate.
5. Agent message persistence and submission do not record the input method and do not accept a transcription `draft_ref`.
6. If a future C implementation offers STT, it must finish before the existing text submission boundary. From TermFlow's perspective, behavior with and without C-side STT is identical.
7. No code path silently falls back from a local C input facility to a server or cloud transcription provider.

## 3. Alternatives considered

### 3.1 Keep server STT but disable it by default — rejected

This preserves audio APIs, capabilities, provider state, deployment secrets, and retention obligations in B. Default-off does not repair the architectural boundary.

### 3.2 Remove B STT but retain the current voice client scaffolding — rejected

The current Tauri and shared-client implementation records and uploads audio to B. Without B's endpoint it becomes misleading dead code rather than a C-side text producer.

### 3.3 Fully excise M7 from 0.2.0 — selected

Remove the server, shared-client, Tauri upload/capture, deployment, schema, contract, documentation, and verification surfaces introduced for M7. Preserve the ordinary text composer and submission path unchanged. A future C-only input method can be designed independently without changing B.

## 4. Implementation boundary

### 4.1 B/control-plane removal

- Remove `api/transcription.py`, the transcription provider port, and `SpeachesTranscriptionProvider`.
- Remove `TERMFLOW_STT_*` settings and provider assembly/shutdown.
- Remove the `speech_to_text_enabled` capability.
- Remove `TranscriptDraft`, its repository, retention sweep, and lifecycle hooks.
- Remove `draft_ref` from message submission and the draft confirmation/consumption transaction.
- Remove STT-specific fixtures and tests.

### 4.2 Schema and contract removal

0.2.0 is not released, so migration `0006_agent_broker.py` must be rewritten to describe the intended 0.2.0 schema directly: no `transcript_drafts` table and no transcription reference on Agent messages. This avoids shipping a create-then-drop schema history for a feature that is not part of the release.

Regenerate the client contracts after the control-plane schemas are corrected. Do not hand-edit generated output.

### 4.3 C/shared-client removal

- Remove transcription API clients, voice draft state, recorder ports, transcript sheet/button UI, capability wiring, and voice-only test harnesses.
- Remove the Tauri audio upload command, recorder/PCM adapters, microphone permissions, manifest patching, and dependencies used only by that path.
- Preserve Agent Chat's existing text composer, text validation, explicit submit action, stream handling, and approval UI.

### 4.4 Deployment and release removal

- Remove the `stt-speaches` Compose profile, STT environment variables, image/model pins, networks, volume, and documentation.
- Remove `verify-stt.sh` and its call from `verify.sh`.
- Remove M7 and STT from the 0.2.0 release gates, required test matrix, reuse audit, and release-readiness claims.
- Mark the superseded M7 design documents clearly so they cannot be mistaken for active specifications; retain them only as decision history.

## 5. Compatibility and data handling

There is no released 0.2.0 STT API or schema to preserve. The feature branch may contain disposable development databases created from the provisional migration; they are not a compatibility contract. The final 0.2.0 upgrade path from the released 0.1.x schema must materialize only the corrected Agent Broker schema.

No migration or cleanup process may inspect, export, or retain provisional raw audio. The existing implementation already stages audio transiently; excision removes that staging surface entirely.

## 6. Verification

The correction is complete only when all of the following are true:

1. Schema/migration tests prove the final 0.2.0 schema contains no transcript-draft table, STT columns, or STT indexes.
2. API/contract tests prove Agent capabilities and message submission expose no STT fields or `draft_ref`.
3. Client tests prove the Agent composer submits the same ordinary text contract without a voice runtime.
4. Compose contract tests prove there is no STT service, profile, credential, model volume, image, or control-plane STT environment variable.
5. Static searches find no active-code reference to the removed STT/audio-upload types. Superseded decision-history documents are the only permitted historical references.
6. Focused control-plane, protocol, client, Tauri, migration, and deployment tests pass.
7. The repository-wide verification suite passes without an STT-specific Docker gate.

## 7. Non-goals

- Selecting or embedding a C-side speech engine.
- Adding platform speech permissions or local model packaging in 0.2.0.
- Sending audio directly from C to a cloud provider.
- Recording the user's input method in B.
- Changing Agent Chat's ordinary text submission semantics.

Any future C-side STT work is a separate client-only design. Its output must enter TermFlow at the same text boundary used by keyboard input.
