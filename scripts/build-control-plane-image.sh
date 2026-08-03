#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
if [[ "$#" -ne 1 || -z "${1}" ]]; then
  echo "usage: build-control-plane-image.sh IMAGE" >&2
  exit 2
fi
IMAGE="${1}"
MAXIMUM_ATTEMPTS="${TERMFLOW_DOCKER_BUILD_ATTEMPTS:-3}"
RETRY_DELAY_SECONDS="${TERMFLOW_DOCKER_BUILD_RETRY_DELAY_SECONDS:-10}"

if [[ ! "${MAXIMUM_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "TERMFLOW_DOCKER_BUILD_ATTEMPTS must be a positive integer." >&2
  exit 2
fi
if [[ ! "${RETRY_DELAY_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "TERMFLOW_DOCKER_BUILD_RETRY_DELAY_SECONDS must be a non-negative integer." >&2
  exit 2
fi

for ((attempt = 1; attempt <= MAXIMUM_ATTEMPTS; attempt += 1)); do
  if docker build \
    -f "${REPOSITORY_ROOT}/deploy/Dockerfile.control-plane" \
    -t "${IMAGE}" \
    "${REPOSITORY_ROOT}"; then
    exit 0
  else
    status=$?
  fi

  if ((status >= 125)); then
    echo "Control Plane image build exited with terminal status ${status} and will not be retried." >&2
    exit "${status}"
  fi

  if ((attempt == MAXIMUM_ATTEMPTS)); then
    echo "Control Plane image build failed after ${MAXIMUM_ATTEMPTS} attempts." >&2
    exit "${status}"
  fi

  echo "Control Plane image build attempt ${attempt}/${MAXIMUM_ATTEMPTS} failed; retrying in ${RETRY_DELAY_SECONDS}s." >&2
  sleep "${RETRY_DELAY_SECONDS}"
done
