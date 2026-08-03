#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -gt 1 ]]; then
  echo "usage: verify_node_bundle.sh [vX.Y.Z]" >&2
  exit 2
fi

TAG="${1:-v0.1.0}"
SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/../.." && pwd)"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/termflow-node-verify.XXXXXX")"
trap 'rm -rf "${temporary}"' EXIT

release_directory="${temporary}/release"
mkdir -p "${release_directory}"
"${SCRIPT_DIRECTORY}/build_node_bundle.sh" "${TAG}" "${release_directory}"
(cd "${release_directory}" && sha256sum termflow-node-linux-x86_64.tar.gz > SHA256SUMS)

installer="${temporary}/install-termflow-node.sh"
uv run --frozen python "${SCRIPT_DIRECTORY}/render_node_installer.py" "${TAG}" "${installer}"
install_home="${temporary}/home"
HOME="${install_home}" \
  TERMFLOW_RELEASE_BASE_URL="file://${release_directory}" \
  bash "${installer}"

installed_node="${install_home}/.local/bin/termflow"
expected_version="${TAG#v}"
if [[ "$("${installed_node}" --version)" != "${expected_version}" ]]; then
  echo "Installed TermFlow executable did not report ${expected_version}." >&2
  exit 1
fi

TERMFLOW_NODE_EXECUTABLE="${installed_node}" \
  uv run --frozen pytest tests/e2e/test_installed_node_e2e.py -q
