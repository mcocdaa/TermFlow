#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 1 || "$#" -gt 2 ]]; then
  echo "usage: build_node_bundle.sh [vX.Y.Z] OUTPUT_DIRECTORY" >&2
  exit 2
fi

if [[ "$#" -eq 2 ]]; then
  TAG="$1"
  OUTPUT_DIRECTORY="$2"
else
  TAG=""
  OUTPUT_DIRECTORY="$1"
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "The TermFlow node bundle can only be built on Linux." >&2
  exit 2
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "The TermFlow node bundle currently supports Linux x86_64 only." >&2
  exit 2
fi

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/../.." && pwd)"
prepare_arguments=(--root "${REPOSITORY_ROOT}")
if [[ -n "${TAG}" ]]; then
  prepare_arguments+=(--tag "${TAG}")
fi
VERSION="$(python "${SCRIPT_DIRECTORY}/prepare_version.py" "${prepare_arguments[@]}")"
ARCHIVE_NAME="termflow-node-linux-x86_64.tar.gz"
ARCHIVE_PATH="${OUTPUT_DIRECTORY}/${ARCHIVE_NAME}"

if [[ -e "${ARCHIVE_PATH}" ]]; then
  echo "Refusing to replace an existing bundle: ${ARCHIVE_PATH}" >&2
  exit 2
fi

mkdir -p "${OUTPUT_DIRECTORY}"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/termflow-node-build.XXXXXX")"
trap 'rm -rf "${BUILD_ROOT}"' EXIT

uv run --frozen --package termflow-node pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name termflow \
  --paths "${REPOSITORY_ROOT}/apps/node/src" \
  --distpath "${BUILD_ROOT}/dist" \
  --workpath "${BUILD_ROOT}/work" \
  --specpath "${BUILD_ROOT}/spec" \
  "${REPOSITORY_ROOT}/apps/node/src/termflow_node/__main__.py"

BUNDLE_DIRECTORY="${BUILD_ROOT}/staging/termflow-node-linux-x86_64"
mkdir -p "${BUNDLE_DIRECTORY}"
cp -a "${BUILD_ROOT}/dist/termflow" "${BUNDLE_DIRECTORY}/termflow"
printf '%s\n' "${VERSION}" > "${BUNDLE_DIRECTORY}/VERSION"

if [[ "$("${BUNDLE_DIRECTORY}/termflow/termflow" --version)" != "${VERSION}" ]]; then
  echo "Frozen TermFlow executable did not report ${VERSION}." >&2
  exit 1
fi

tar -C "${BUILD_ROOT}/staging" -czf "${BUILD_ROOT}/${ARCHIVE_NAME}" termflow-node-linux-x86_64
mv "${BUILD_ROOT}/${ARCHIVE_NAME}" "${ARCHIVE_PATH}"
printf 'Built %s\n' "${ARCHIVE_PATH}"
