#!/usr/bin/env bash
set -euo pipefail

attempts="${TERMFLOW_APPIMAGE_BUILD_ATTEMPTS:-3}"
retry_delay="${TERMFLOW_APPIMAGE_RETRY_DELAY_SECONDS:-10}"
output_dir="${TERMFLOW_APPIMAGE_OUTPUT_DIR:-apps/clients/tauri/src-tauri/target/release/bundle/appimage}"
tauri_cache="${XDG_CACHE_HOME:-${HOME}/.cache}/tauri"

if ! [[ "$attempts" =~ ^[1-9][0-9]*$ ]]; then
  echo "TERMFLOW_APPIMAGE_BUILD_ATTEMPTS must be a positive integer" >&2
  exit 2
fi
if ! [[ "$retry_delay" =~ ^[0-9]+$ ]]; then
  echo "TERMFLOW_APPIMAGE_RETRY_DELAY_SECONDS must be a non-negative integer" >&2
  exit 2
fi
case "$output_dir" in
  */target/release/bundle/appimage) ;;
  *)
    echo "TERMFLOW_APPIMAGE_OUTPUT_DIR must end with /target/release/bundle/appimage" >&2
    exit 2
    ;;
esac

report_tauri_tool_cache() {
  local tool name size digest

  if [[ ! -d "$tauri_cache" ]]; then
    echo "Tauri tool cache directory is absent" >&2
    return
  fi

  while IFS= read -r -d '' tool; do
    name="$(basename "$tool")"
    if ! size="$(stat -c '%s' "$tool")"; then
      size='unavailable'
    fi
    if digest="$(sha256sum "$tool")"; then
      digest="${digest%% *}"
    else
      digest='unavailable'
    fi
    printf 'Tauri tool cache: name=%s size=%s sha256=%s\n' \
      "$name" "$size" "$digest" >&2
  done < <(find "$tauri_cache" -maxdepth 1 -type f -print0 | sort -z)
}

status=1
for ((attempt = 1; attempt <= attempts; attempt++)); do
  echo "Building AppImage (attempt $attempt/$attempts)" >&2
  set +e
  npm run tauri:build --workspace @termflow/tauri-client -- \
    --bundles appimage --ci --verbose
  status=$?
  set -e

  if (( status == 0 )); then
    exit 0
  fi

  echo "AppImage attempt $attempt/$attempts failed with exit status $status" >&2
  report_tauri_tool_cache

  if [[ -d "$output_dir" ]]; then
    rm -rf -- "$output_dir"
  fi

  if (( attempt == attempts )); then
    break
  fi

  echo "Retrying AppImage build in ${retry_delay}s" >&2
  sleep "$retry_delay"
done

echo "AppImage build exhausted $attempts AppImage build attempts" >&2
exit "$status"
