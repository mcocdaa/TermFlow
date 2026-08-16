# Speaches — Pinned STT Container Compatibility Contract

Status: **Pinned fixture** for TermFlow 0.2.0 Agent Broker milestone M7a.

This document is the pinned speaches container contract for the first optional
STT provider (plan §14/§16; spec `2026-08-13-m7a-stt-container-design.md`). It
records the pinned image tag+digest (CPU and CUDA variants), the
OpenAI-compatible transcription endpoint contract, the `/health` endpoint, the
`API_KEY` Bearer auth model, the container environment table, the pinned
default model, the license, telemetry defaults, and the internal-network
no-egress model-volume pre-seed requirement. The machine-readable contract is
the embedded `termflow-speaches-pin-contract` JSON block at the end; the prose
below is the human-readable contract and is the source of truth for that
block.

The pin was re-captured from the ghcr.io registry API (`ghcr.io/v2/
speaches-ai/speaches/manifests/<tag>`, token scope `repository:
speaches-ai/speaches:pull`) on **2026-08-13** during implementation:

- `0.8.3-cpu` → `sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8`
- `0.8.3-cuda` → `sha256:9abc6968a883e77ead9286c38a6ccf3139dcad518b783deeebb7be2ced50e972`

Both match the values captured at spec time (no drift). `latest-cpu` currently
resolves to the same digest as `0.8.3-cpu`. The digests are re-captured again
at the M8 gate and any drift updates this fixture and re-runs the container
integration tests.

---

## 1. Pinned image

- **Image:** `ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8`
  (tag + digest double pin; `latest*` tags are forbidden by the compose
  contract test).
- **CUDA variant (deployment follow-up, not shipped):**
  `ghcr.io/speaches-ai/speaches:0.8.3-cuda@sha256:9abc6968a883e77ead9286c38a6ccf3139dcad518b783deeebb7be2ced50e972`.
  A GPU run profile is a later deployment option; this spec ships the CPU
  pinned image only.
- **License:** MIT (speaches-ai/speaches, active upstream).
- **Runtime posture:** non-root `ubuntu` user (UID 1000), `EXPOSE 8000`,
  `/health` requires the Bearer token (M7 exit re-capture: 403 without it,
  200 with `Authorization: Bearer <API_KEY>` — the earlier "public" note
  was wrong), image ships `curl` and `ffmpeg` (webm/wav/mpeg all
  supported).

## 2. Endpoint contract (OpenAI-compatible)

- `POST /v1/audio/transcriptions` — multipart/form-data:
  - `file=(<safe_filename>, audio, mime_type)` — filenames are
    mime-derived by B's provider: `audio/wav → speech.wav`,
    `audio/webm → speech.webm`, `audio/mpeg → speech.mp3`.
  - `model=<model id>` — the configured `STT_MODEL`.
  - `response_format=json` — explicit; the response is the OpenAI default
    JSON shape `{"text": "<transcript>"}`.
  - `Authorization: Bearer <token>` — only when a token is configured (the
    container's `API_KEY` env); `/health` also requires the Bearer token
    (M7 exit re-capture).
- `GET /health` — liveness used by the compose healthcheck (must send
  `Authorization: Bearer <API_KEY>`); not used by B for per-request probing
  (availability is configuration-derived, spec §5.3).

## 3. Container environment table

| Env | Value | Meaning |
|---|---|---|
| `UVICORN_PORT` | `8000` | uvicorn listen port (matches `EXPOSE`) |
| `ENABLE_UI` | `false` | disable the Gradio UI |
| `LOG_LEVEL` | `warning` | compact logs (model loads are chatty) |
| `STT_MODEL_TTL` | `-1` | keep the single preloaded model resident |
| `PRELOAD_MODELS` | `["<model>"]` (JSON array) | **must be a JSON array** — speaches' `preload_models: list[str]` parses complex env as JSON; a bare string fails startup. Startup validates the model exists and exits on a miss (fail-loud). |
| `WHISPER__COMPUTE_TYPE` | `int8` | CPU-friendly compute type |
| `API_KEY` | required secret (`${STT_API_KEY:?}`) | Bearer token for `/v1/*`; required deployment secret, never a committed value |

## 4. Model pin and pre-seed contract

- **Pinned default model:** `Systran/faster-distil-whisper-small.en` —
  English-only distilled small Whisper model, CPU-friendly latency/memory in
  int8; multilingual deployments override with `STT_MODEL` (a new model
  requires re-seeding the volume).
- **No-egress constraint:** the `agent_internal` network is `internal: true`,
  so the container cannot download models from HuggingFace at runtime.
  Operators must pre-seed the `stt-models` volume once, on a default
  (egress-capable) network, before first enable:

  ```bash
  docker run --rm \
    -v stt-models:/home/ubuntu/.cache/huggingface/hub \
    ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8 \
    python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-distil-whisper-small.en')"
  ```

  A fresh named volume inherits ownership from the image's pre-created
  `/home/ubuntu/.cache/huggingface/hub` (UID 1000), so the default non-root
  user can write it. The exact command is also frozen by
  `scripts/verify-stt.sh`.

- **Telemetry defaults:** the image bakes `DO_NOT_TRACK`,
  `DISABLE_TELEMETRY`, and `HF_HUB_DISABLE_TELEMETRY`; no additional opt-out
  env is needed.

## 5. B-side integration notes

- Provider: `plugins/agent_broker/agent/speaches.py` (`SpeachesTranscriptionProvider`),
  wired by `TERMFLOW_STT_*` settings; errors map to
  `TranscriptionProviderError` (502) with stable-category logs only; raw
  audio is never logged, persisted, or retained (port §14).
- Enable contract (double explicit opt-in): `docker compose --profile stt up -d`
  **and** `TERMFLOW_STT_ENABLED=true` **and** `STT_API_KEY` set; otherwise the
  container never runs and B answers 503 `speech_to_text_unavailable`.
- `read_only` rootfs: the container runs with `read_only: true` + `/tmp`
  tmpfs in the compose profile; a live-container verification (M7 exit / M8)
  confirms speaches works under the full hardened parameter set. If
  read-only breaks a runtime write path, the fallback is to drop only
  `read_only` and record the deviation here (never silently relax).

## 6. Verification status

- Provider/config/compose-contract/assembly tests: **done** (no Docker).
- Live container verification under the final compose hardening (digest
  pull, CapEff, non-root, read-only rootfs, healthcheck, real transcription
  round trip, model pre-seed command): **unknown** — runs at the M7 exit /
  M8 gate via `scripts/verify-stt.sh` (Docker-gated; environments without
  Docker record `unverified`, never inferred as passing).

---

```json termflow-speaches-pin-contract
{
  "provider": "speaches",
  "image": {
    "registry": "ghcr.io",
    "repository": "speaches-ai/speaches",
    "cpu_tag": "0.8.3-cpu",
    "cpu_digest": "sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8",
    "cuda_tag": "0.8.3-cuda",
    "cuda_digest": "sha256:9abc6968a883e77ead9286c38a6ccf3139dcad518b783deeebb7be2ced50e972",
    "license": "MIT",
    "latest_cpu_resolves_to": "0.8.3-cpu digest at 2026-08-13 capture",
    "digest_recaptured": "2026-08-13 (ghcr.io registry API; no drift from spec capture)",
    "latest_tags_forbidden": true
  },
  "endpoints": {
    "transcriptions": "POST /v1/audio/transcriptions (multipart file/model/response_format=json)",
    "health": "GET /health (requires Authorization: Bearer <API_KEY>; used by compose healthcheck only)",
    "auth": "Authorization: Bearer <token> when API_KEY is configured; optional for B"
  },
  "environment": {
    "UVICORN_PORT": "8000",
    "ENABLE_UI": "false",
    "LOG_LEVEL": "warning",
    "STT_MODEL_TTL": "-1",
    "PRELOAD_MODELS": "JSON array of model ids (pydantic-settings complex env); bare string fails startup",
    "WHISPER__COMPUTE_TYPE": "int8",
    "API_KEY": "required deployment secret (compose ${STT_API_KEY:?}); /health requires the Bearer token"
  },
  "model": {
    "default": "Systran/faster-distil-whisper-small.en",
    "override": "STT_MODEL (requires re-seeding the model volume)",
    "download_requirement": "agent_internal has no egress; the stt-models volume must be pre-seeded once with snapshot_download on an egress-capable network"
  },
  "privacy": {
    "telemetry": "DO_NOT_TRACK, DISABLE_TELEMETRY, HF_HUB_DISABLE_TELEMETRY baked into the image",
    "raw_audio": "never logged, persisted, or retained by B's provider (port §14)"
  },
  "verification": {
    "no_docker_tests": "provider/config/compose-contract/assembly done",
    "live_container": "unknown until M7 exit / M8 via scripts/verify-stt.sh; Docker-less environments record unverified"
  }
}
```
