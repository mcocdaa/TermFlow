#!/usr/bin/env bash
# M7a STT container integration verification (spec §7.3) — Docker-gated.
#
# Proves the pinned speaches container against the exact hardening used by
# deploy/compose.yaml, then runs B end to end:
#
#   1. pull the tag+digest pinned image and prove the digest still matches
#      the pin fixture (a pull failure is a hard failure — placeholders and
#      drift fail loud);
#   2. pre-seed the model volume with the frozen snapshot_download command
#      (spec §4.4) — this script is where that command is executable;
#   3. start a one-shot container with the compose hardening (internal
#      network, no published ports, cap_drop ALL, no-new-privileges,
#      read_only rootfs + tmpfs, user 1000:1000, model volume) and inspect
#      CapEff == 0, non-root PID 1, read-only rootfs, and a passing /health
#      check;
#   4. end to end through compose (COMPOSE_PROJECT_NAME=termflow-verify-stt,
#      host port 18765): with `--profile stt` and the double opt-in
#      (TERMFLOW_STT_ENABLED=true + STT_API_KEY) B uploads a real WAV → 201
#      draft with provider "speaches"; the same deployment WITHOUT the
#      profile answers 503 speech_to_text_unavailable.
#
# Docker-gated: without a usable Docker daemon the script prints an explicit
# UNVERIFIED record and exits 0 — an unavailable environment is never
# inferred as passing (M8 contract).
#
# The default WAV is a vowel-formant approximation; operators may supply a
# real speech sample via STT_VERIFY_WAV=/path/to/speech.wav for the E2E.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPOSITORY_ROOT}"

# The host-side curl probes must bypass any ambient HTTP proxy (a proxied
# localhost request can 502); the compose services talk over internal
# networks and are unaffected.
export no_proxy="localhost,127.0.0.1"
export NO_PROXY="localhost,127.0.0.1"

#: Pinned image and digest (spec §4.2; tests/fixtures/speaches/speaches-pin.md).
STT_IMAGE="ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8"
STT_DIGEST="sha256:21e3df06d842fb7802ab470dd77c25f0e8c0d22950e8d8c6ae886e851af53ef8"
STT_MODEL="${STT_MODEL:-Systran/faster-distil-whisper-small.en}"
# Exported: the four E2E `docker compose` calls interpolate STT_API_KEY
# from the process environment (${STT_API_KEY:?} applies to the whole
# compose file), so a plain shell assignment would never reach them.
export STT_API_KEY="${STT_API_KEY:-verify-stt-api-key}"
# The pre-seed/hardened containers use the exact volume name that compose
# resolves for its `stt-models` declaration (project prefix + reference),
# so the model cache lands on the same volume the compose service mounts.
COMPOSE_PROJECT="termflow-verify-stt"
STT_MODELS_VOLUME="stt-models"
STT_MODELS_VOLUME_REAL="${COMPOSE_PROJECT}_${STT_MODELS_VOLUME}"
STT_NETWORK="termflow-verify-stt-net"
STT_CONTAINER="termflow-verify-stt-$$"
STT_HOST_PORT="${STT_HOST_PORT:-18765}"

verify_tmp="$(mktemp -d)"
cleanup() {
  docker rm --force "${STT_CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${STT_NETWORK}" >/dev/null 2>&1 || true
  docker volume rm "${STT_MODELS_VOLUME_REAL}" >/dev/null 2>&1 || true
  rm -rf "${verify_tmp}"
}
trap cleanup EXIT

if ! docker info >/dev/null 2>&1; then
  echo "verify-stt: Docker daemon unavailable; STT live verification SKIPPED — UNVERIFIED (M8 contract: an unavailable environment is not inferred as passing)." >&2
  exit 0
fi

json_field() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

echo "== verify-stt: pulling pinned image and checking the digest against the pin fixture"
grep -Fq "${STT_DIGEST}" "${REPOSITORY_ROOT}/apps/control-plane/tests/fixtures/speaches/speaches-pin.md" \
  || { echo "verify-stt: pinned digest drifted from tests/fixtures/speaches/speaches-pin.md" >&2; exit 1; }
docker pull "${STT_IMAGE}"

echo "== verify-stt: pre-seeding the model volume (frozen command, spec §4.4)"
docker volume create "${STT_MODELS_VOLUME_REAL}" >/dev/null
docker run --rm \
  --volume "${STT_MODELS_VOLUME_REAL}:/home/ubuntu/.cache/huggingface/hub" \
  "${STT_IMAGE}" \
  python -c "from huggingface_hub import snapshot_download; snapshot_download('${STT_MODEL}')"

echo "== verify-stt: one-shot hardened container (compose parameters, spec §4.2)"
# A scratch internal network with the same parameter as the compose
# agent_internal network (internal: true, no egress, no published ports).
docker network create --internal "${STT_NETWORK}" >/dev/null
docker run --detach \
  --name "${STT_CONTAINER}" \
  --network "${STT_NETWORK}" \
  --user 1000:1000 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:size=64m,mode=1777,noexec,nosuid \
  --volume "${STT_MODELS_VOLUME_REAL}:/home/ubuntu/.cache/huggingface/hub" \
  --env UVICORN_PORT=8000 \
  --env ENABLE_UI=false \
  --env LOG_LEVEL=warning \
  --env STT_MODEL_TTL=-1 \
  --env PRELOAD_MODELS="[\"${STT_MODEL}\"]" \
  --env WHISPER__COMPUTE_TYPE=int8 \
  --env API_KEY="${STT_API_KEY}" \
  --health-cmd "curl -fsS -H \"Authorization: Bearer \$API_KEY\" http://127.0.0.1:8000/health" \
  --health-interval 10s \
  --health-timeout 3s \
  --health-retries 3 \
  --health-start-period 30s \
  "${STT_IMAGE}" >/dev/null

for attempt in $(seq 1 45); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "${STT_CONTAINER}" 2>/dev/null || true)"
  [[ "${status}" == "healthy" ]] && break
  sleep 2
done
test "$(docker inspect --format '{{.State.Health.Status}}' "${STT_CONTAINER}")" = "healthy" \
  || { echo "verify-stt: speaches /health never became healthy" >&2; exit 1; }

# Hardening proofs on the live container (mirrors verify-node-image.sh):
test "$(docker exec "${STT_CONTAINER}" awk '$1 == "CapEff:" { print $2 }' /proc/1/status)" = "0000000000000000" \
  || { echo "verify-stt: PID 1 retained capabilities (cap_drop ALL failed)" >&2; exit 1; }
test "$(docker exec "${STT_CONTAINER}" stat -c %u /proc/1)" = "1000" \
  || { echo "verify-stt: PID 1 is not the fixed non-root UID 1000" >&2; exit 1; }
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${STT_CONTAINER}")" = "true" \
  || { echo "verify-stt: rootfs is not read-only" >&2; exit 1; }
test -z "$(docker port "${STT_CONTAINER}")" \
  || { echo "verify-stt: container published a host port" >&2; exit 1; }
docker exec "${STT_CONTAINER}" curl -fsS -H "Authorization: Bearer ${STT_API_KEY}" http://127.0.0.1:8000/health >/dev/null \
  || { echo "verify-stt: /health did not answer inside the container" >&2; exit 1; }

docker rm --force "${STT_CONTAINER}" >/dev/null
docker network rm "${STT_NETWORK}" >/dev/null

echo "== verify-stt: E2E — compose --profile stt, real WAV upload -> 201 draft"
export TERMFLOW_ADMIN_TOKEN="${TERMFLOW_ADMIN_TOKEN:-verify-admin-token-that-is-long-enough}"
export OPENCODE_SERVER_USERNAME="verify-opencode-user"
export OPENCODE_SERVER_PASSWORD="verify-opencode-password"
export OPENCODE_MODEL_API_KEY="verify-opencode-key"
export STT_MODELS_VOLUME
export TERMFLOW_HOST_PORT="${STT_HOST_PORT}"
# Docker Desktop (WSL2) resets WSL-side connections to loopback-bound port
# mappings; bind 0.0.0.0 for the verification deployment (deployments keep
# the compose loopback default).
export TERMFLOW_HOST_BIND="${TERMFLOW_HOST_BIND:-0.0.0.0}"

wav_file="${verify_tmp}/speech.wav"
if [[ -n "${STT_VERIFY_WAV:-}" ]]; then
  cp "${STT_VERIFY_WAV}" "${wav_file}"
else
  python3 - > "${wav_file}" <<'PY'
import math, struct, sys, wave

SAMPLE_RATE = 16000
DURATION = 1.0
# Vowel-formant approximation: 120 Hz glottal-pulse train shaped by
# 700/1100/2600 Hz formants, so the E2E transcribes a speech-like signal
# instead of silence (a pure tone is reliably transcribed as empty text).
frames = bytearray()
for i in range(int(SAMPLE_RATE * DURATION)):
    t = i / SAMPLE_RATE
    glottal = sum(
        (1.0 / harmonic) * math.sin(2 * math.pi * 120.0 * harmonic * t)
        for harmonic in range(1, 9)
    )
    sample = (
        math.sin(2 * math.pi * 700.0 * t) * 0.6
        + math.sin(2 * math.pi * 1100.0 * t) * 0.3
        + math.sin(2 * math.pi * 2600.0 * t) * 0.1
    ) * glottal * 0.08
    frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
with wave.open(sys.stdout.buffer, "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(SAMPLE_RATE)
    wav.writeframes(frames)
PY
fi

# The double opt-in (spec §4.2): profile + TERMFLOW_STT_ENABLED=true +
# STT_API_KEY. opencode-agent is never started (its image is the M8
# placeholder), so only the named services are brought up; the control-plane
# image is built by compose on first use.
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT}" \
  TERMFLOW_STT_ENABLED=true \
  docker compose -f deploy/compose.yaml --profile stt up -d --build control-plane stt-speaches >/dev/null

for attempt in $(seq 1 150); do
  curl -fsS "http://127.0.0.1:${STT_HOST_PORT}/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:${STT_HOST_PORT}/healthz" >/dev/null \
  || { echo "verify-stt: control-plane /healthz never answered" >&2; exit 1; }

admin_curl() {
  curl -fsS -H "Authorization: Bearer ${TERMFLOW_ADMIN_TOKEN}" "$@"
}

enrollment_token="$(admin_curl -X POST \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/enrollment-tokens" | json_field token)"
installed="$(curl -fsS -X POST -H "Content-Type: application/json" \
  -d "{\"enrollment_token\": \"${enrollment_token}\"}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/installations/enroll")"
installation_token="$(printf '%s' "${installed}" | json_field installation_token)"
term_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -fsS -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${installation_token}" \
  -d "{\"instance_id\": \"${term_id}\", \"name\": \"stt-verify\"}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/instances/register" >/dev/null
profile="$(admin_curl -X POST -H "Content-Type: application/json" \
  -d '{"display_name": "stt-verify", "backend_kind": "opencode", "config": "{\"model\": \"default\"}"}' \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/agent/admin/profiles")"
profile_id="$(printf '%s' "${profile}" | json_field profile_id)"
binding="$(admin_curl -X POST -H "Content-Type: application/json" \
  -d "{\"profile_id\": \"${profile_id}\", \"term_id\": \"${term_id}\"}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/agent/admin/bindings")"
binding_id="$(printf '%s' "${binding}" | json_field binding_id)"
conversation="$(admin_curl -X POST -H "Content-Type: application/json" \
  -d "{\"binding_id\": \"${binding_id}\"}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/agent/conversations")"
conversation_id="$(printf '%s' "${conversation}" | json_field conversation_id)"

upload_http_code="$(curl -sS -o "${verify_tmp}/upload.json" -w '%{http_code}' \
  -H "Authorization: Bearer ${TERMFLOW_ADMIN_TOKEN}" \
  -F "audio=@${wav_file};type=audio/wav" \
  -F "binding_id=${binding_id}" \
  -F "target_conversation_id=${conversation_id}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/agent/transcription/drafts")"
test "${upload_http_code}" = "201" \
  || { echo "verify-stt: profile-stage upload returned ${upload_http_code} (expected 201): $(cat "${verify_tmp}/upload.json")" >&2; exit 1; }
test "$(json_field provider < "${verify_tmp}/upload.json")" = "speaches" \
  || { echo "verify-stt: draft provider is not 'speaches'" >&2; exit 1; }
test "$(json_field state < "${verify_tmp}/upload.json")" = "draft" \
  || { echo "verify-stt: draft state is not 'draft'" >&2; exit 1; }

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT}" \
  docker compose -f deploy/compose.yaml --profile stt down --volumes >/dev/null

echo "== verify-stt: E2E — no-profile deployment upload -> 503"
# TERMFLOW_STT_ENABLED is deliberately unset: the stt profile is absent, the
# container never runs, and B keeps the Null provider (spec §5.2). The 503 is
# emitted before any repository lookup, so the previous stage's UUIDs are
# reused as form values.
env -u TERMFLOW_STT_ENABLED \
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT}" \
  docker compose -f deploy/compose.yaml up -d --build control-plane >/dev/null

for attempt in $(seq 1 150); do
  curl -fsS "http://127.0.0.1:${STT_HOST_PORT}/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:${STT_HOST_PORT}/healthz" >/dev/null \
  || { echo "verify-stt: no-profile control-plane /healthz never answered" >&2; exit 1; }

no_profile_http_code="$(curl -sS -o "${verify_tmp}/upload-503.json" -w '%{http_code}' \
  -H "Authorization: Bearer ${TERMFLOW_ADMIN_TOKEN}" \
  -F "audio=@${wav_file};type=audio/wav" \
  -F "binding_id=${binding_id}" \
  -F "target_conversation_id=${conversation_id}" \
  "http://127.0.0.1:${STT_HOST_PORT}/api/v1/agent/transcription/drafts")"
test "${no_profile_http_code}" = "503" \
  || { echo "verify-stt: no-profile upload returned ${no_profile_http_code} (expected 503)" >&2; exit 1; }
grep -Fq "speech_to_text_unavailable" "${verify_tmp}/upload-503.json" \
  || { echo "verify-stt: no-profile upload did not report speech_to_text_unavailable" >&2; exit 1; }

env -u TERMFLOW_STT_ENABLED \
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT}" \
  docker compose -f deploy/compose.yaml down --volumes >/dev/null

echo "verify-stt: OK — pinned digest verified, hardening proved, E2E 201/503 as designed"
