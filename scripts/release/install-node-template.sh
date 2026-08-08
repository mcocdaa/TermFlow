#!/usr/bin/env bash
set -euo pipefail

readonly TAG="@TAG@"
readonly VERSION="${TAG#v}"
readonly ARCHIVE="termflow-node-linux-x86_64.tar.gz"

fail() {
  echo "TermFlow install failed: $*" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "this installer supports Linux only"
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  fail "this installer currently supports x86_64 only"
fi
for requirement in curl sha256sum tmux tar; do
  command -v "${requirement}" >/dev/null 2>&1 || fail "missing required command: ${requirement}"
done

if ! tmux_version="$(tmux -V | sed -nE 's/^tmux ([0-9]+)\.([0-9]+).*/\1 \2/p')"; then
  fail "could not determine tmux version"
fi
read -r tmux_major tmux_minor <<< "${tmux_version}"
if [[ -z "${tmux_major:-}" || -z "${tmux_minor:-}" ]] || ((tmux_major < 3 || (tmux_major == 3 && tmux_minor < 2))); then
  fail "tmux 3.2 or newer is required"
fi

readonly RELEASE_BASE="${TERMFLOW_RELEASE_BASE_URL:-https://github.com/@REPOSITORY@/releases/download/${TAG}}"
readonly PREFIX="${HOME:?HOME must be set}/.local"
readonly VERSION_DIRECTORY="${PREFIX}/opt/termflow-node/${TAG}"
readonly BIN_DIRECTORY="${PREFIX}/bin"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/termflow-install.XXXXXX")"
trap 'rm -rf "${temporary}"' EXIT

curl --fail --location --silent --show-error "${RELEASE_BASE%/}/${ARCHIVE}" --output "${temporary}/${ARCHIVE}"
curl --fail --location --silent --show-error "${RELEASE_BASE%/}/SHA256SUMS" --output "${temporary}/SHA256SUMS"
(cd "${temporary}" && grep -F "  ${ARCHIVE}" SHA256SUMS | sha256sum --check --status) \
  || fail "checksum verification failed"

if command -v gh >/dev/null 2>&1; then
  if ! gh attestation verify "${temporary}/${ARCHIVE}" --repo "@REPOSITORY@" >/dev/null 2>&1; then
    fail "GitHub provenance attestation verification failed; refusing to install"
  fi
else
  echo "TermFlow note: GitHub CLI is not installed; provenance attestation was not verified." >&2
fi

mkdir -p "${temporary}/extract"
tar -xzf "${temporary}/${ARCHIVE}" -C "${temporary}/extract"
candidate="${temporary}/extract/termflow-node-linux-x86_64"
[[ -x "${candidate}/termflow/termflow" ]] || fail "release archive has no executable"
[[ -f "${candidate}/VERSION" ]] || fail "release archive has no VERSION"
IFS= read -r installed_version < "${candidate}/VERSION"
[[ "${installed_version}" == "${VERSION}" ]] || fail "release archive version does not match ${TAG}"

mkdir -p "${PREFIX}/opt/termflow-node" "${BIN_DIRECTORY}"
if [[ -e "${VERSION_DIRECTORY}" ]]; then
  [[ -x "${VERSION_DIRECTORY}/termflow/termflow" ]] \
    || fail "existing ${TAG} installation is invalid"
  backup="${VERSION_DIRECTORY}.old.$$"
  if ! mv "${VERSION_DIRECTORY}" "${backup}" || ! mv "${candidate}" "${VERSION_DIRECTORY}"; then
    mv "${backup}" "${VERSION_DIRECTORY}" 2>/dev/null || true
    fail "could not replace existing ${TAG} installation"
  fi
  rm -rf "${backup}"
else
  mv "${candidate}" "${VERSION_DIRECTORY}"
fi

temporary_link="${BIN_DIRECTORY}/.termflow-${TAG}.tmp"
ln -s "../opt/termflow-node/${TAG}/termflow/termflow" "${temporary_link}"
mv -Tf "${temporary_link}" "${BIN_DIRECTORY}/termflow"
printf 'Installed TermFlow %s at %s\n' "${VERSION}" "${BIN_DIRECTORY}/termflow"
