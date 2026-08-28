# Public Edge and Node Transport Hardening Implementation Plan

**Implementation status (2026-08-28):** The Node and repository verification changes are implemented and pass `scripts/verify.sh`. Live deployment acceptance remains open: the public origin currently serves plaintext HTTP with status 200 and emits duplicate HSTS headers over HTTPS, so `scripts/security/verify-public-edge.sh` fails. No server login or configuration mutation was performed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public TermFlow origin HTTPS-only, reject public plaintext Control Plane URLs in every Node path, and add repeatable live checks for the deployed edge.

**Architecture:** The public reverse proxy owns canonical HTTP-to-HTTPS redirection and overwrites forwarding headers. The Node accepts plaintext only for literal loopback hosts and no longer persists an override. A repository script tests the external edge so source verification and deployment acceptance remain separate and auditable.

**Tech Stack:** Nginx, Bash, curl, Python 3.12, Pydantic 2, pytest

---

## Security invariants

- `http://termflow.mcocdaa-newapi.xin/...` returns `308` to the same path and query on `https://termflow.mcocdaa-newapi.xin` without reflecting the request `Host` into `Location`.
- The TLS virtual host emits exactly one HSTS header and overwrites `Host`, `X-Forwarded-Proto`, and `X-Forwarded-For` before proxying.
- A Node may use `http://127.0.0.1`, `http://localhost`, or `http://[::1]`; every other HTTP URL is rejected with no bypass flag.
- Legacy local config containing `"allow_insecure_http": false` migrates on read; `true` fails closed with an actionable error and is never written again.
- Passing the repository test suite is not live acceptance; the external verification script must also pass against the deployed domain.

### Task 1: Lock the Node HTTPS policy with failing tests

**Files:**
- Modify: `apps/node/tests/test_login.py`
- Modify: `apps/node/tests/test_config_store.py`
- Modify: `apps/node/tests/test_bridge_transport.py`
- Modify: `apps/node/tests/test_diagnostics.py`
- Modify: `apps/node/tests/integration/test_tmux_lifecycle.py`

- [ ] **Step 1: Replace the public-HTTP opt-in test with an unconditional rejection test**

In `apps/node/tests/test_login.py`, import `validate_server_url` and replace the
public-HTTP opt-in test with tests of the final public API:

```python
def test_validate_server_url_rejects_public_http_without_override() -> None:
    with pytest.raises(InsecureServerUrl, match="require HTTPS"):
        validate_server_url("http://192.0.2.10:8080")


@pytest.mark.parametrize(
    "server_url",
    [
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://[::1]:8080",
    ],
)
def test_validate_server_url_allows_only_loopback_http(server_url: str) -> None:
    assert validate_server_url(server_url) == server_url
```

- [ ] **Step 2: Add legacy-config migration and fail-closed tests**

In `apps/node/tests/test_config_store.py`, import `Path`, write owner-only JSON fixtures, and assert:

```python
def test_load_migrates_legacy_false_insecure_http_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "https://termflow.example",
                "installation_id": str(uuid4()),
                "installation_token": "secret",
                "allow_insecure_http": False,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    loaded = ConfigStore(path).load()

    assert not hasattr(loaded, "allow_insecure_http")


def test_load_rejects_legacy_true_insecure_http_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "http://192.0.2.10:8080",
                "installation_id": str(uuid4()),
                "installation_token": "secret",
                "allow_insecure_http": True,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(
        InsecureConfigError,
        match="no longer supports public HTTP; run `termflow login` with an HTTPS URL",
    ):
        ConfigStore(path).load()
```

Also update the save assertion so the serialized object has exactly `server_url`, `installation_id`, and `installation_token`.

- [ ] **Step 3: Remove tests that expect `--allow-insecure-http` and add a CLI rejection**

In `apps/node/tests/test_login.py`, add:

```python
def test_login_rejects_removed_allow_insecure_http_option() -> None:
    result = CliRunner().invoke(
        app,
        [
            "login",
            "--server",
            "http://192.0.2.10:8080",
            "--allow-insecure-http",
        ],
    )

    assert result.exit_code != 0
    assert "No such option: --allow-insecure-http" in result.output
```

Update bridge tests so `bridge_websocket_url("http://192.0.2.10")` raises and no call passes an override argument.

- [ ] **Step 4: Run the focused tests and observe the intended failures**

Run:

```bash
.envs/dev/bin/python -m pytest \
  apps/node/tests/test_login.py \
  apps/node/tests/test_config_store.py \
  apps/node/tests/test_bridge_transport.py \
  apps/node/tests/test_diagnostics.py \
  apps/node/tests/integration/test_tmux_lifecycle.py -q
```

Expected: failures mention the still-present `allow_insecure_http` parameter, model field, CLI option, or serialized key.

### Task 2: Remove the Node plaintext override and migrate local config

**Files:**
- Modify: `apps/node/src/termflow_node/control_plane_client.py`
- Modify: `apps/node/src/termflow_node/bridge/transport.py`
- Modify: `apps/node/src/termflow_node/bridge/runtime.py`
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/src/termflow_node/diagnostics.py`
- Modify: `apps/node/src/termflow_node/config/models.py`
- Modify: `apps/node/src/termflow_node/config/store.py`
- Modify: all test fakes found by `rg -n "allow_insecure_http" apps/node`

- [ ] **Step 1: Make URL validation unconditional**

Replace the validator signature and condition in `control_plane_client.py`:

```python
def validate_server_url(server_url: str) -> str:
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InsecureServerUrl("Server URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InsecureServerUrl("Server URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise InsecureServerUrl("Non-loopback TermFlow servers require HTTPS")
    return server_url.rstrip("/")
```

Remove `allow_insecure_http` from `enroll`, `probe_health`, every call to `validate_server_url`, and every test fake matching those signatures.

- [ ] **Step 2: Remove the bridge override**

Change `bridge_websocket_url` to accept only the server URL. It must call `validate_server_url(server_url)` and map `https` to `wss` and loopback `http` to `ws`. Remove the override from bridge runtime construction and all callers.

- [ ] **Step 3: Remove the persisted model field with an explicit migration gate**

Delete `allow_insecure_http` from `InstallationConfig`. In `ConfigStore.load`, parse JSON before Pydantic validation:

```python
        raw = json.loads(self.path.read_bytes())
        legacy_override = raw.pop("allow_insecure_http", False)
        if legacy_override is not False:
            raise InsecureConfigError(
                "TermFlow no longer supports public HTTP; run `termflow login` "
                "with an HTTPS URL"
            )
        return InstallationConfig.model_validate(raw)
```

In `save`, remove the `allow_insecure_http` key. Keep `ConfigDict(extra="forbid")` so unknown security flags still fail closed.

- [ ] **Step 4: Delete the CLI option and derive status from the scheme**

Remove the Typer option, warning, and all forwarding of `allow_insecure_http`. Replace `_transport_insecure` with a scheme check that reports loopback plaintext without making it configurable:

```python
def _transport_insecure(installation: InstallationConfig) -> bool:
    return urlsplit(str(installation.server_url)).scheme == "http"
```

- [ ] **Step 5: Verify there is no residual bypass**

Run:

```bash
rg -n "allow_insecure_http|allow-insecure-http" apps/node docs deploy
.envs/dev/bin/python -m pytest apps/node/tests -q
```

Expected: `rg` returns no matches after the documentation task below; Node tests pass.

- [ ] **Step 6: Commit the Node policy change**

```bash
git add -- apps/node
git commit -m "security: require HTTPS for non-loopback nodes"
```

### Task 3: Document one non-contradictory transport policy

**Files:**
- Modify: `docs/security.md`
- Modify: `docs/operations.md`
- Modify: `tests/docs/test_documentation_contract.py`

- [ ] **Step 1: Add a failing documentation contract**

Add a test that asserts the security guide contains `Plaintext HTTP is supported only on literal loopback hosts` and contains neither `--allow-insecure-http` nor `trusted LAN`.

- [ ] **Step 2: Rewrite the Node transport section**

State exactly:

```markdown
Plaintext HTTP is supported only on literal loopback hosts (`127.0.0.1`,
`localhost`, and `::1`) for local development. Every non-loopback Control Plane
URL requires HTTPS. There is no command-line or persisted-config override.
```

In operations documentation, distinguish three checks: source tests, container checks, and live public-edge checks.

- [ ] **Step 3: Run the documentation contract and commit**

```bash
.envs/dev/bin/python -m pytest tests/docs/test_documentation_contract.py -q
git add -- docs/security.md docs/operations.md tests/docs/test_documentation_contract.py
git commit -m "docs: define the HTTPS-only public transport boundary"
```

### Task 4: Add a public-edge acceptance script

**Files:**
- Create: `scripts/security/verify-public-edge.sh`
- Create: `tests/security/test_public_edge_script.py`
- Modify: `docs/operations.md`

- [ ] **Step 1: Add a failing repository contract for the script**

Add a test that reads the script and requires all of these literal controls: `308`, `Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, and `/.well-known/oauth-authorization-server`.

- [ ] **Step 2: Implement the live verifier**

Create an executable Bash script with this complete behavior:

```bash
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
```

- [ ] **Step 3: Make the script executable and run static checks**

```bash
chmod 0755 scripts/security/verify-public-edge.sh
bash -n scripts/security/verify-public-edge.sh
.envs/dev/bin/python -m pytest tests/security/test_public_edge_script.py -q
```

- [ ] **Step 4: Document and commit the verifier**

Document that it uses the network and must be run after deployment, then commit:

```bash
git add -- scripts/security/verify-public-edge.sh docs/operations.md \
  tests/security/test_public_edge_script.py
git commit -m "test: add public edge security acceptance"
```

### Task 5: Correct the external Nginx deployment

**Files:**
- Modify on edge host: `/etc/nginx/conf.d/termflow.mcocdaa-newapi.xin.conf`
- Verify only: `scripts/security/verify-public-edge.sh`

- [ ] **Step 1: Back up and inspect the exact active virtual host**

On the edge host, run:

```bash
sudo cp -a \
  /etc/nginx/conf.d/termflow.mcocdaa-newapi.xin.conf \
  /etc/nginx/conf.d/termflow.mcocdaa-newapi.xin.conf.before-https-only
sudo nginx -T
```

Confirm there is one HTTP virtual host and one TLS virtual host for the exact domain. Do not reload if another file also owns the same `server_name`.

- [ ] **Step 2: Install the canonical HTTP redirect**

The port 80 block must be exactly equivalent to:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name termflow.mcocdaa-newapi.xin;

    return 308 https://termflow.mcocdaa-newapi.xin$request_uri;
}
```

The hard-coded canonical authority prevents hostile `Host` values from being reflected into the redirect.

- [ ] **Step 3: Harden the TLS proxy block**

In the existing TLS server, retain the certificate directives and ensure the proxy location contains:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_hide_header Strict-Transport-Security;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host termflow.mcocdaa-newapi.xin;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

The `proxy_hide_header` line suppresses the application's HSTS value and the
edge emits the single canonical value above. Do not add another HSTS directive
in a parent Nginx context.

- [ ] **Step 4: Validate, reload, and perform live acceptance**

```bash
sudo nginx -t
sudo systemctl reload nginx
./scripts/security/verify-public-edge.sh termflow.mcocdaa-newapi.xin
curl -sS -o /dev/null -D - \
  -H 'Host: attacker.invalid' \
  http://termflow.mcocdaa-newapi.xin/security-probe
```

Expected: the script prints `public edge verified`; the hostile-Host probe still redirects to `https://termflow.mcocdaa-newapi.xin/security-probe`.

### Task 6: Final regression and evidence capture

**Files:**
- Verify only: repository and live deployment

- [ ] **Step 1: Run the full repository verifier with the project Node toolchain**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH ./scripts/verify.sh
```

Expected: JavaScript, Python, Rust, build, and Docker verification sections all pass.

- [ ] **Step 2: Run live acceptance again after the application deployment**

```bash
./scripts/security/verify-public-edge.sh termflow.mcocdaa-newapi.xin
```

Record the deployed commit SHA, Nginx config checksum, command output, and timestamp. Do not describe the deployment as accepted without this second, post-deploy result.
