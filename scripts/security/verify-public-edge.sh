#!/usr/bin/env bash
set -euo pipefail

host="${1:-termflow.mcocdaa-newapi.xin}"
http_headers="$(mktemp)"
https_headers="$(mktemp)"
oauth_body="$(mktemp)"
trap 'rm -f "$http_headers" "$https_headers" "$oauth_body"' EXIT

curl --silent --show-error --max-time 10 \
  --dump-header "$http_headers" --output /dev/null \
  "http://${host}/security-probe?value=1"

http_status="$(awk 'NR == 1 { print $2 }' "$http_headers")"
location="$(awk 'tolower($1) == "location:" { sub(/\r$/, "", $2); print $2 }' "$http_headers")"
[[ "$http_status" == "308" ]] || { echo "expected HTTP 308, got ${http_status}" >&2; exit 1; }
[[ "$location" == "https://${host}/security-probe?value=1" ]] || {
  echo "unexpected redirect location: ${location}" >&2
  exit 1
}

curl --silent --show-error --max-time 10 \
  --dump-header "$https_headers" --output /dev/null "https://${host}/"

for name in Strict-Transport-Security Content-Security-Policy \
  X-Content-Type-Options Referrer-Policy X-Frame-Options; do
  count="$(awk -v wanted="$name" 'tolower($1) == tolower(wanted ":") { count++ } END { print count + 0 }' "$https_headers")"
  [[ "$count" == "1" ]] || { echo "expected exactly one ${name}, got ${count}" >&2; exit 1; }
done

curl --silent --show-error --fail --max-time 10 \
  "https://${host}/.well-known/oauth-authorization-server" >"$oauth_body"
python3 - "$oauth_body" "https://${host}" <<'PY'
import json
import pathlib
import sys

body = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if body.get("issuer") != sys.argv[2]:
    raise SystemExit(f"unexpected OAuth issuer: {body.get('issuer')!r}")
PY

echo "public edge verified: https://${host}"
