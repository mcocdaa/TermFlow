# TermFlow Settings and TOTP Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the revised shared Settings UI, one env-authoritative relay URL, theme-colored QR dialogs, a Web-only TOTP onboarding page, and independently configurable/enabled TOTP login protection.

**Architecture:** Python protocol models remain the DTO source, B derives enrollment commands and OAuth issuer from `TERMFLOW_PUBLIC_BASE_URL`, and `client-core` exposes the generated API to shared Vue UI. Authentication persistence stores a configured encrypted authenticator independently from `totp_enabled_at`; Web-only routes toggle enforcement after fresh primary and TOTP verification. Default single-instance Compose auto-provisions a persistent master-key file while explicit env/Docker secret sources retain priority.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic, Vue 3, TypeScript, Vue Router, qrcode SVG rendering, Vitest, Pytest, Docker Compose, Playwright browser tests.

---

## File responsibility map

- `packages/protocol/src/termflow_protocol/http.py`: public enrollment and TOTP request/response DTOs.
- `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`: atomic configured/enabled/counter/epoch state transitions.
- `apps/control-plane/src/termflow_control_plane/auth/service.py`: primary credential, TOTP decryption, verification, and state-transition orchestration.
- `apps/control-plane/src/termflow_control_plane/api/security.py`: Web-only TOTP HTTP endpoints, rate limits, audit, and no-store responses.
- `apps/control-plane/src/termflow_control_plane/auth/master_key.py`: explicit-key resolution and atomic default key-file creation.
- `apps/control-plane/src/termflow_control_plane/api/enrollment.py`: env-authoritative enrollment response and login command.
- `packages/client-core/src/api/security.ts`: typed TOTP setup and login-protection calls.
- `packages/client-ui/src/components/common/ThemedQrCode.vue`: one theme-reactive SVG QR renderer for Server and TOTP.
- `packages/client-ui/src/components/common/QrCodeDialog.vue`: accessible QR modal behavior.
- `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`: relay-address presentation, copy, and QR trigger.
- `packages/client-ui/src/components/settings/TotpPanel.vue`: status summary, activation navigation, and enforcement toggle.
- `packages/client-ui/src/components/settings/TotpProtectionDialog.vue`: step-up dialog for enable/disable operations.
- `packages/client-ui/src/views/TotpActivationView.vue`: Web-only authenticator onboarding workflow.
- `packages/client-ui/src/router/routes.ts`: route capability metadata shared by Web and filtered by Tauri.
- `packages/client-ui/src/styles/app.css`: Settings, responsive theme grid, QR, wizard, and switch layout.

### Task 1: Extend protocol and generated client contracts

**Files:**
- Modify: `packages/protocol/src/termflow_protocol/http.py`
- Modify: `packages/protocol/src/termflow_protocol/__init__.py`
- Modify: `packages/protocol/tests/test_http_models.py`
- Modify: `tests/contracts/test_client_contract_generation.py`
- Regenerate: `packages/client-contracts/src/generated.ts`

- [ ] **Step 1: Write failing DTO tests for env-derived enrollment data and configured TOTP state**

Add assertions equivalent to:

```python
response = EnrollmentCreateResponse(
    token="join-" + "x" * 40,
    expires_at=datetime.now(UTC),
    server_url="https://relay.example.com",
    login_command=(
        "termflow login --server https://relay.example.com "
        "--code join-" + "x" * 40
    ),
)
assert response.server_url == "https://relay.example.com"
assert response.login_command.startswith("termflow login --server")
assert response.login_command not in repr(response)

status = TotpStatusResponse(configured=True, enabled=False, available=True)
assert status.model_dump() == {
    "configured": True,
    "enabled": False,
    "available": True,
}
with pytest.raises(ValidationError):
    TotpStatusResponse(configured=False, enabled=True, available=True)
```

Add a request-model test proving `TotpProtectionRequest(admin_token="admin-" + "a" * 32, code="123456")` redacts both secrets from `repr` and rejects non-six-digit codes.

- [ ] **Step 2: Run protocol tests and verify the new models fail**

Run:

```bash
uv run --package termflow-protocol pytest packages/protocol/tests/test_http_models.py -q
```

Expected: FAIL because `EnrollmentCreateResponse` lacks `server_url`/`login_command`, `TotpStatusResponse` lacks `configured`, and `TotpProtectionRequest` is not exported.

- [ ] **Step 3: Implement the strict protocol models**

Change the DTOs to the following shape and export the new request:

```python
class EnrollmentCreateResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime
    server_url: str = Field(min_length=1, max_length=2048)
    login_command: str = Field(repr=False, min_length=1, max_length=4096)


class TotpStatusResponse(HttpModel):
    configured: bool
    enabled: bool
    available: bool

    @model_validator(mode="after")
    def enabled_requires_configuration(self) -> "TotpStatusResponse":
        if self.enabled and not self.configured:
            raise ValueError("enabled TOTP must be configured")
        return self


class TotpProtectionRequest(HttpModel):
    admin_token: SecretStr
    code: SecretStr

    @field_validator("code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr:
        return validate_totp_code(value)
```

Retain `TotpDisableRequest` only as a compatibility alias if an existing import still requires it; new code must use `TotpProtectionRequest`.

- [ ] **Step 4: Regenerate TypeScript contracts and verify drift checks**

Run:

```bash
npm run contracts:generate
npm run contracts:check
uv run --package termflow-protocol pytest packages/protocol/tests/test_http_models.py tests/contracts/test_client_contract_generation.py -q
```

Expected: generated `TotpStatusResponse` has `configured`, enrollment has both server fields, all selected tests PASS.

- [ ] **Step 5: Commit the contract slice**

```bash
git add packages/protocol packages/client-contracts/src/generated.ts tests/contracts/test_client_contract_generation.py
git commit -m "feat(protocol): separate configured and enabled TOTP state"
```

### Task 2: Make enrollment commands use the public relay env URL

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/api/enrollment.py`
- Modify: `apps/control-plane/tests/test_enrollment_api.py`
- Modify: `packages/client-ui/src/components/computers/EnrollmentDialog.vue`
- Modify: `packages/client-ui/src/components/computers/EnrollmentDialog.test.ts`

- [ ] **Step 1: Write failing API and component tests for one canonical relay URL**

In the API test, create settings with `public_base_url="https://relay.example.com"` and assert:

```python
body = response.json()
assert body["server_url"] == "https://relay.example.com"
assert body["login_command"] == (
    f"termflow login --server https://relay.example.com --code {body['token']}"
)
```

In `EnrollmentDialog.test.ts`, make `createEnrollment` return `server_url` and `login_command`, set `runtime.canonicalServerUrl` to a deliberately different origin, and assert the displayed/copied command is exactly the server response.

- [ ] **Step 2: Run the focused tests and verify they fail for the expected source mismatch**

Run:

```bash
uv run pytest apps/control-plane/tests/test_enrollment_api.py -q
npm run test:run --workspace @termflow/client-ui -- EnrollmentDialog.test.ts
```

Expected: API response fields are absent and the component still constructs the command from `runtime.canonicalServerUrl`.

- [ ] **Step 3: Generate both enrollment fields in B and consume them unchanged in C**

In the endpoint use:

```python
server_url = str(settings.public_base_url).rstrip("/")
return EnrollmentCreateResponse(
    token=raw_token,
    expires_at=expires_at,
    server_url=server_url,
    login_command=f"termflow login --server {server_url} --code {raw_token}",
)
```

In the Vue component store `enrollment.login_command` in a local ref, clear it together with the one-time token, and remove the computed command based on `runtime.canonicalServerUrl`.

- [ ] **Step 4: Run enrollment API, component, generated-contract, and type checks**

```bash
uv run pytest apps/control-plane/tests/test_enrollment_api.py -q
npm run test:run --workspace @termflow/client-ui -- EnrollmentDialog.test.ts
npm run contracts:check
npm run typecheck --workspace @termflow/client-ui
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the relay URL slice**

```bash
git add apps/control-plane/src/termflow_control_plane/api/enrollment.py apps/control-plane/tests/test_enrollment_api.py packages/client-ui/src/components/computers/EnrollmentDialog.vue packages/client-ui/src/components/computers/EnrollmentDialog.test.ts
git commit -m "feat(enrollment): return env-authoritative login command"
```

### Task 3: Separate configured authenticators from login enforcement

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/service.py`
- Modify: `apps/control-plane/tests/test_auth_persistence.py`
- Modify: `apps/control-plane/tests/test_totp.py`

- [ ] **Step 1: Write failing repository transition tests**

Cover this exact trajectory:

```python
assert await repositories.auth_state.configure_totp(
    encrypted,
    counter=100,
    expected_epoch=1,
    expected_generation=0,
)
configured = await repositories.auth_state.get()
assert configured.totp_ciphertext == encrypted.ciphertext
assert configured.totp_enabled_at is None

assert await repositories.auth_state.enable_totp_protection(
    counter=101,
    expected_epoch=configured.epoch,
    expected_generation=configured.totp_generation,
)
enabled = await repositories.auth_state.get()
assert enabled.totp_enabled_at is not None

assert await repositories.auth_state.disable_totp_protection(
    expected_epoch=enabled.epoch,
    expected_generation=enabled.totp_generation,
)
disabled = await repositories.auth_state.get()
assert disabled.totp_enabled_at is None
assert disabled.totp_ciphertext == encrypted.ciphertext
assert disabled.totp_last_accepted_counter == 101
```

Also assert stale generation/counter transitions fail and `reset_and_increment_epoch()` is the only transition that clears ciphertext.

- [ ] **Step 2: Run persistence tests and verify the transition API is missing**

```bash
uv run pytest apps/control-plane/tests/test_auth_persistence.py -q
```

Expected: FAIL with missing `configure_totp`, `enable_totp_protection`, and `disable_totp_protection`.

- [ ] **Step 3: Implement atomic persistence transitions**

Implement three focused methods with these state transitions (imports are already present in
`repositories.py`):

```python
async def configure_totp(
    self,
    encrypted: EncryptedSecret,
    counter: int,
    *,
    expected_epoch: int,
    expected_generation: int,
    enabled: bool,
) -> bool:
    observed_at = datetime.now(UTC)
    async with self._sessions() as session:
        result = await session.execute(
            update(AuthenticationState)
            .where(
                AuthenticationState.id == 1,
                AuthenticationState.epoch == expected_epoch,
                AuthenticationState.totp_generation == expected_generation,
            )
            .values(
                totp_ciphertext=encrypted.ciphertext,
                totp_nonce=encrypted.nonce,
                totp_key_version=encrypted.key_version,
                totp_aad_version=encrypted.aad_version,
                totp_enabled_at=observed_at if enabled else None,
                totp_last_accepted_counter=counter,
                totp_generation=AuthenticationState.totp_generation + 1,
                updated_at=observed_at,
            )
            .returning(AuthenticationState.id)
        )
        configured = result.scalar_one_or_none() is not None
        await session.commit()
        return configured


async def enable_totp_protection(
    self,
    counter: int,
    *,
    expected_epoch: int,
    expected_generation: int,
) -> bool:
    observed_at = datetime.now(UTC)
    async with self._sessions() as session:
        result = await session.execute(
            update(AuthenticationState)
            .where(
                AuthenticationState.id == 1,
                AuthenticationState.epoch == expected_epoch,
                AuthenticationState.totp_generation == expected_generation,
                AuthenticationState.totp_ciphertext.is_not(None),
                AuthenticationState.totp_nonce.is_not(None),
                AuthenticationState.totp_key_version.is_not(None),
                AuthenticationState.totp_aad_version.is_not(None),
                AuthenticationState.totp_enabled_at.is_(None),
                or_(
                    AuthenticationState.totp_last_accepted_counter.is_(None),
                    AuthenticationState.totp_last_accepted_counter < counter,
                ),
            )
            .values(
                totp_enabled_at=observed_at,
                totp_last_accepted_counter=counter,
                totp_generation=AuthenticationState.totp_generation + 1,
                updated_at=observed_at,
            )
            .returning(AuthenticationState.id)
        )
        enabled_now = result.scalar_one_or_none() is not None
        await session.commit()
        return enabled_now


async def disable_totp_protection(
    self,
    *,
    expected_epoch: int,
    expected_generation: int,
) -> bool:
    observed_at = datetime.now(UTC)
    async with self._sessions() as session:
        result = await session.execute(
            update(AuthenticationState)
            .where(
                AuthenticationState.id == 1,
                AuthenticationState.epoch == expected_epoch,
                AuthenticationState.totp_generation == expected_generation,
                AuthenticationState.totp_enabled_at.is_not(None),
            )
            .values(
                totp_enabled_at=None,
                totp_generation=AuthenticationState.totp_generation + 1,
                updated_at=observed_at,
            )
            .returning(AuthenticationState.id)
        )
        disabled_now = result.scalar_one_or_none() is not None
        await session.commit()
        return disabled_now
```

`configure_totp` replaces ciphertext, records the confirmation counter, preserves `enabled_at` only for an enabled reconfiguration, and increments generation. `enable_totp_protection` requires complete ciphertext, `enabled_at IS NULL`, and a counter newer than the persisted counter. `disable_totp_protection` requires `enabled_at IS NOT NULL`, clears only `enabled_at`, and retains ciphertext/counter. Each transition increments TOTP generation to invalidate stale challenges, but only container reset increments authentication epoch and revokes existing sessions/tokens.

- [ ] **Step 4: Write failing service tests for configure, enable, disable, and reconfigure while disabled**

Use a fixed clock and secrets to prove:

```python
assert await service.confirm_totp_setup(setup.setup_id, first_code)
assert await service.totp_status() == (True, False, True)
admin_token = "admin-token-that-is-long-enough-for-tests"
assert not await service.enable_totp(admin_token, first_code)
assert await service.enable_totp(admin_token, next_code)
assert await service.totp_status() == (True, True, True)
assert await service.disable_totp(admin_token, later_code)
assert await service.totp_status() == (True, False, True)
```

Begin a replacement setup while disabled and require the current authenticator code before returning new setup material.

- [ ] **Step 5: Run service tests and verify old immediate-enable behavior fails**

```bash
uv run pytest apps/control-plane/tests/test_totp.py -q
```

Expected: FAIL because confirmation currently enables immediately and disabled state cannot decrypt/verify a configured secret.

- [ ] **Step 6: Implement configured-secret verification and explicit protection methods**

Refactor `_enabled_secret` into `_configured_secret`, make login paths additionally require `totp_enabled_at`, and expose these concrete service contracts:

```python
async def totp_status(self) -> tuple[bool, bool, bool]:
    state = await self._repositories.auth_state.get()
    configured = self._has_configured_secret(state)
    return configured, state.totp_enabled_at is not None, self._secret_box is not None


async def enable_totp(self, admin_token: str, code: str) -> bool:
    if not self.primary_token_matches(admin_token):
        raise AuthenticationRejected
    return await self._set_totp_protection(code, enabled=True)


async def disable_totp(self, admin_token: str, code: str) -> bool:
    if not self.primary_token_matches(admin_token):
        raise AuthenticationRejected
    return await self._set_totp_protection(code, enabled=False)
```

Both toggle methods verify the primary token and a fresh code against the configured secret. Setup replacement uses configured-secret verification even when protection is off. Confirmation calls `configure_totp` and never sets `enabled_at`.

- [ ] **Step 7: Run persistence and service suites**

```bash
uv run pytest apps/control-plane/tests/test_auth_persistence.py apps/control-plane/tests/test_totp.py -q
```

Expected: all selected tests PASS with no plaintext secret in database/repr assertions.

- [ ] **Step 8: Commit the state-machine slice**

```bash
git add apps/control-plane/src/termflow_control_plane/persistence/repositories.py apps/control-plane/src/termflow_control_plane/auth/service.py apps/control-plane/tests/test_auth_persistence.py apps/control-plane/tests/test_totp.py
git commit -m "feat(auth): decouple TOTP setup from login protection"
```

### Task 4: Expose Web-only TOTP toggle endpoints and retain recovery boundaries

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/api/security.py`
- Modify: `apps/control-plane/tests/test_totp_api.py`
- Modify: `apps/control-plane/tests/test_cli_tokens.py`
- Modify: `tests/e2e/test_unified_auth.py`

- [ ] **Step 1: Rewrite API tests around configured and enabled states**

Change the setup helper so confirmation expects:

```python
assert confirmed.json() == {
    "configured": True,
    "enabled": False,
    "available": True,
}
```

Then POST `/api/v1/admin/totp/enable` with `admin_token` and a fresh `code`, expect enabled true; DELETE `/api/v1/admin/totp` with another fresh code, expect configured true/enabled false. Assert replay, wrong Origin, wrong primary token, and missing cookie fail without changing state. Verify disabled configured TOTP does not change Web/CLI/OAuth login, while enabled TOTP does.

- [ ] **Step 2: Run TOTP API tests and verify current endpoints/status fail**

```bash
uv run pytest apps/control-plane/tests/test_totp_api.py apps/control-plane/tests/test_cli_tokens.py -q
```

Expected: FAIL because confirm returns enabled true and `/enable` does not exist.

- [ ] **Step 3: Implement status, enable, and disable HTTP behavior**

Return all three status booleans from GET and confirmation. Add:

Use `@router.post("/enable", response_model=TotpStatusResponse)` for enable and
`@router.delete("", response_model=TotpStatusResponse)` for disable. Both handlers accept
`TotpProtectionRequest`, `Request`, `Response`, and the injected `AuthenticationService`;
they call the corresponding service method inside `limiter.verification_slot()`, record one
success or failure under their distinct limiter operation, emit the existing authentication
audit result, apply `_no_store(response)`, and return enabled true from POST and enabled false from
DELETE, with `configured=True, available=True` in both responses.

Use separate limiter operation names, exact-Origin Web-cookie dependencies, no-store headers, generic `authentication_failed` errors, and authentication audit records. Return `configured=True` on both successful toggles. Preserve absence of reset/recovery HTTP routes.

- [ ] **Step 4: Update real-process authentication trajectory**

Change `tests/e2e/test_unified_auth.py` to execute and assert:

```text
setup confirm -> normal login works -> enable with fresh code -> TOTP login required
-> disable with fresh code -> normal login works -> container CLI reset clears configuration
```

Retain the existing replay rejection and credential redaction checks.

- [ ] **Step 5: Run API, CLI, OAuth, sessions, and real-process tests**

```bash
uv run pytest apps/control-plane/tests/test_totp_api.py apps/control-plane/tests/test_cli_tokens.py apps/control-plane/tests/test_oauth_api.py apps/control-plane/tests/test_browser_sessions.py tests/e2e/test_unified_auth.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit the API slice**

```bash
git add apps/control-plane/src/termflow_control_plane/api/security.py apps/control-plane/tests/test_totp_api.py apps/control-plane/tests/test_cli_tokens.py tests/e2e/test_unified_auth.py
git commit -m "feat(auth): add explicit TOTP login protection toggle"
```

### Task 5: Auto-provision a persistent key for default single-instance Compose

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/auth/master_key.py`
- Create: `apps/control-plane/tests/test_totp_master_key.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/env.example`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `docs/operations.md`

- [ ] **Step 1: Write failing key-resolution tests**

Test explicit env bytes, explicit Docker-secret file, and automatic file in this precedence order. For the automatic path assert first resolution creates 43-character unpadded base64url, mode `0o600`, 32 decoded bytes, and the second resolution returns identical bytes. Use concurrent calls/process-safe `O_CREAT | O_EXCL` behavior and assert no raw key appears in `repr` or exceptions.

- [ ] **Step 2: Run key tests and verify the resolver is absent**

```bash
uv run pytest apps/control-plane/tests/test_totp_master_key.py -q
```

Expected: FAIL because `auth.master_key` and `totp_auto_master_key_file` do not exist.

- [ ] **Step 3: Implement explicit-first atomic key resolution**

Add `Settings.totp_auto_master_key_file: Path | None = None`. Implement:

```python
def resolve_totp_master_key(settings: Settings) -> bytes | None:
    explicit = settings.totp_master_key_bytes
    if explicit is not None:
        return explicit
    if settings.totp_auto_master_key_file is None:
        return None
    return _read_or_create_key_file(settings.totp_auto_master_key_file)
```

Use `secrets.token_bytes(32)`, unpadded base64url storage, `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`, `fsync`, exact-length write checks, and a read-after-exists race path. Reject an existing file accessible to group/other and never replace it automatically.

- [ ] **Step 4: Load the resolved key during app lifespan and configure Compose**

Replace `settings.totp_master_key_bytes` in `app.py` with the resolver. In default Compose add:

```yaml
TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE: /app/data/totp-master-key
```

Keep `TERMFLOW_TOTP_MASTER_KEY` and `TERMFLOW_TOTP_MASTER_KEY_FILE` as higher-priority overrides. Update operator docs to say auto-provision applies only to the default single-instance data volume; multi-B deployments must provide one shared explicit key.

- [ ] **Step 5: Run key, config, Compose, and image contract tests**

```bash
uv run pytest apps/control-plane/tests/test_totp_master_key.py apps/control-plane/tests/test_config.py tests/deploy/test_compose_contract.py tests/docs/test_documentation_contract.py -q
docker compose -f deploy/compose.yaml config --quiet
```

Expected: all tests PASS and Compose validation exits 0 without printing a generated key.

- [ ] **Step 6: Commit the key-management slice**

```bash
git add apps/control-plane/src/termflow_control_plane/auth/master_key.py apps/control-plane/src/termflow_control_plane/config.py apps/control-plane/src/termflow_control_plane/app.py apps/control-plane/tests/test_totp_master_key.py deploy/compose.yaml deploy/env.example tests/deploy/test_compose_contract.py docs/operations.md
git commit -m "feat(deploy): auto-provision persistent TOTP master key"
```

### Task 6: Add shared theme-colored SVG QR components

**Files:**
- Modify: `packages/design-tokens/src/themes/graphite-signal.css`
- Modify: `packages/design-tokens/src/themes/cloud-cobalt.css`
- Modify: `packages/design-tokens/src/themes/midnight-indigo.css`
- Modify: `packages/design-tokens/src/contract.test.ts`
- Create: `packages/client-ui/src/components/common/ThemedQrCode.vue`
- Create: `packages/client-ui/src/components/common/ThemedQrCode.test.ts`
- Create: `packages/client-ui/src/components/common/QrCodeDialog.vue`
- Create: `packages/client-ui/src/components/common/QrCodeDialog.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing token and QR rendering tests**

Require `--color-qr-foreground` and `--color-qr-background` in every theme. Mock `QRCode.toString`, select each theme through `ThemeState`, and assert the call uses:

```ts
{
  type: 'svg',
  errorCorrectionLevel: 'M',
  margin: 2,
  color: { dark: resolvedForeground, light: resolvedBackground },
}
```

Assert the rendered image uses a `data:image/svg+xml` URL. For the dialog assert `role="dialog"`, `aria-modal="true"`, close button, Escape, backdrop click, and focus return to the QR trigger.

- [ ] **Step 2: Run design-token and QR tests and verify missing components/tokens fail**

```bash
npm run test:run --workspace @termflow/design-tokens
npm run test:run --workspace @termflow/client-ui -- ThemedQrCode.test.ts QrCodeDialog.test.ts
```

Expected: FAIL because tokens/components do not exist.

- [ ] **Step 3: Implement semantic QR tokens and the renderer**

Set theme-specific high-contrast QR foreground/background values in theme files. `ThemedQrCode.vue` must watch both `props.value` and `useTheme().active`, read only the two semantic CSS properties from `document.documentElement`, call `QRCode.toString`, encode the returned SVG into a data URL, and render it via `<img>` without `v-html`.

- [ ] **Step 4: Implement the accessible QR dialog**

The dialog receives `open`, `title`, `value`, and `description`; emits `close`; closes on Escape/backdrop; focuses its close button on open; and returns focus to the supplied trigger element on close. Use the shared QR renderer inside the dialog.

- [ ] **Step 5: Run QR, token, a11y, and type checks**

```bash
npm run test:run --workspace @termflow/design-tokens
npm run test:run --workspace @termflow/client-ui -- ThemedQrCode.test.ts QrCodeDialog.test.ts a11y-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: all commands exit 0 and the literal-color architecture guard remains clean.

- [ ] **Step 6: Commit the shared QR slice**

```bash
git add packages/design-tokens packages/client-ui/src/components/common packages/client-ui/src/styles/app.css
git commit -m "feat(client): add theme-colored QR components"
```

### Task 7: Rebuild Settings, Server, and theme layout

**Files:**
- Modify: `packages/client-ui/src/views/SettingsView.vue`
- Modify: `packages/client-ui/src/views/SettingsView.test.ts`
- Modify: `packages/client-ui/src/components/settings/ThemePicker.vue`
- Modify: `packages/client-ui/src/components/settings/ThemePicker.test.ts`
- Modify: `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`
- Create: `packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing Settings and Server presentation tests**

Assert the page has `Settings` and `设置`, omits the old explanatory sentence and `B 连接地址`, and renders `Server / 中继服务器 / 服务网址`. Assert the URL is the metadata issuer, remains read-only, copy uses `runtime.clipboard`, the QR icon has accessible name “显示服务网址二维码”, and clicking it opens the shared themed QR dialog with a credential-free versioned payload.

Add a ThemePicker DOM contract asserting every option is a direct child of a grid-marked radiogroup and uses a full-width class; add CSS contract assertions for `grid-template-columns: repeat(auto-fit, minmax(min(100%, 10rem), 1fr))` and removal of `width: fit-content`.

- [ ] **Step 2: Run focused UI tests and verify old copy/layout fails**

```bash
npm run test:run --workspace @termflow/client-ui -- SettingsView.test.ts ServerConnectionPanel.test.ts ThemePicker.test.ts
```

Expected: FAIL on old headings, inline always-visible QR, and fit-content layout.

- [ ] **Step 3: Implement the requested Settings and Server hierarchy**

Use `Settings` for the page eyebrow, remove the paragraph, rename the server heading, add the “服务网址” subheading plus Lucide `QrCode` SVG button, keep the code field and copy button on one row, and show the shared dialog only after activation. Continue loading OAuth metadata once from Settings and pass its env-derived issuer to the panel.

- [ ] **Step 4: Implement the scalable full-width theme grid**

Change Settings-scoped `.theme-picker` to width `100%`, CSS grid, and `repeat(auto-fit, minmax(min(100%, 10rem), 1fr))`; give each `.theme-option` width `100%` and retain radio/arrow-key behavior. Add the narrow-screen one-column rule only when the minimum width cannot fit.

- [ ] **Step 5: Run Settings, responsive, a11y, and type checks**

```bash
npm run test:run --workspace @termflow/client-ui -- SettingsView.test.ts ServerConnectionPanel.test.ts ThemePicker.test.ts responsive-contract.test.ts a11y-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit the Settings layout slice**

```bash
git add packages/client-ui/src/views/SettingsView.vue packages/client-ui/src/views/SettingsView.test.ts packages/client-ui/src/components/settings/ThemePicker.vue packages/client-ui/src/components/settings/ThemePicker.test.ts packages/client-ui/src/components/settings/ServerConnectionPanel.vue packages/client-ui/src/components/settings/ServerConnectionPanel.test.ts packages/client-ui/src/styles/app.css
git commit -m "feat(client): revise Settings and relay server panel"
```

### Task 8: Add the Web-only TOTP onboarding and login-protection switch

**Files:**
- Modify: `packages/client-core/src/api/security.ts`
- Create: `packages/client-core/src/api/security.test.ts`
- Modify: `packages/client-ui/src/components/settings/TotpPanel.vue`
- Create: `packages/client-ui/src/components/settings/TotpPanel.test.ts`
- Create: `packages/client-ui/src/components/settings/TotpProtectionDialog.vue`
- Create: `packages/client-ui/src/components/settings/TotpProtectionDialog.test.ts`
- Create: `packages/client-ui/src/views/TotpActivationView.vue`
- Create: `packages/client-ui/src/views/TotpActivationView.test.ts`
- Modify: `packages/client-ui/src/router/routes.ts`
- Modify: `packages/client-ui/src/index.ts`
- Modify: `apps/clients/tauri/src/router.ts`
- Create: `apps/clients/tauri/src/router.test.ts`
- Modify: `apps/clients/web/src/router.test.ts`
- Modify: `packages/client-ui/src/styles/app.css`

- [ ] **Step 1: Write failing client-core API tests for explicit toggles**

Assert `enableTotpProtection` posts `{admin_token, code}` to `/api/v1/admin/totp/enable`, `disableTotpProtection` sends the same body with DELETE to `/api/v1/admin/totp`, and setup confirmation returns configured but disabled status.

- [ ] **Step 2: Run client-core tests and verify toggle methods are absent**

```bash
npm run test:run --workspace @termflow/client-core -- security.test.ts
```

Expected: FAIL with missing methods or wrong request paths.

- [ ] **Step 3: Add typed toggle methods to client-core**

Expose:

```ts
enableTotpProtection(reauth: Required<SecurityReauthentication>, signal?: AbortSignal)
disableTotpProtection(reauth: Required<SecurityReauthentication>, signal?: AbortSignal)
```

Both return `TotpStatusResponse`; keep all field-name translation inside `client-core`.

- [ ] **Step 4: Write failing TOTP panel, dialog, onboarding, and router tests**

Cover:

- unconfigured panel shows `Two Factor Authentication / 双重因素认证` and “激活双重因素认证”;
- no master-key environment variable/path text appears;
- activation navigates to `/settings/two-factor-auth`;
- wizard requires admin token, renders themed setup QR/manual key, confirms code, then shows an off switch;
- toggling on opens step-up fields and requires a fresh code before reporting enabled;
- configured disabled panel shows bound status plus off switch; configured enabled shows on switch;
- a failed toggle leaves the switch state unchanged and shows a generic error;
- Web router contains the Web-only route; Tauri router filters `meta.webOnly` routes rather than only hiding navigation.

- [ ] **Step 5: Run UI/router tests and verify the inline old flow fails**

```bash
npm run test:run --workspace @termflow/client-ui -- TotpPanel.test.ts TotpProtectionDialog.test.ts TotpActivationView.test.ts
npm run test:run --workspace @termflow/web-client -- router.test.ts
npm run test:run --workspace @termflow/tauri-client -- router.test.ts
```

Expected: FAIL because the wizard route, status switch, and Tauri route filter are absent.

- [ ] **Step 6: Implement the summary panel and secure toggle dialog**

`TotpPanel.vue` loads status, emits the full status to Settings, navigates to the wizard when unconfigured, and opens `TotpProtectionDialog` when the switch changes. The switch UI changes only after a successful API response. The dialog always requires administrator Token and six-digit code, clears both on close/success, and uses generic product-facing errors.

- [ ] **Step 7: Implement the onboarding view and Web-only route**

Build the five-step flow from the design. After setup confirmation set local status to `{configured:true, enabled:false, available:true}` and require a new code to turn protection on. Mark the route:

```ts
{
  path: '/settings/two-factor-auth',
  component: TotpActivationView,
  meta: { requiresAuth: true, webOnly: true },
}
```

Filter `route.meta.webOnly === true` from the Tauri route table. Do not rely on a hidden button as an authorization boundary; Tauri runtime capability and server Web-cookie/Origin checks remain in force.

- [ ] **Step 8: Run UI, route, type, and build checks**

```bash
npm run test:run --workspace @termflow/client-core -- security.test.ts
npm run test:run --workspace @termflow/client-ui -- TotpPanel.test.ts TotpProtectionDialog.test.ts TotpActivationView.test.ts SettingsView.test.ts
npm run test:run --workspace @termflow/web-client -- router.test.ts
npm run test:run --workspace @termflow/tauri-client -- router.test.ts
npm run typecheck
npm run build --workspaces --if-present
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit the TOTP UI slice**

```bash
git add packages/client-core/src/api packages/client-ui/src apps/clients/web/src/router.test.ts apps/clients/tauri/src/router.ts apps/clients/tauri/src/router.test.ts
git commit -m "feat(client): add TOTP onboarding and protection switch"
```

### Task 9: Perform isolated browser and full verification

**Files:**
- Create: `apps/clients/web/e2e/settings-auth.spec.ts`
- Modify: `scripts/run-web-e2e.sh`
- Modify: `scripts/verify.sh` only if the new targeted suite is not already included by existing workspace/Pytest discovery

- [ ] **Step 1: Extend the browser test before changing browser fixtures**

Add a disposable-server trajectory that logs in, verifies Settings copy/layout and three equal theme controls, opens the themed relay QR, activates an authenticator, confirms configured-disabled state, enables protection with a fresh code, logs out/in through the TOTP challenge, disables protection, and reloads to prove configured-disabled persistence. Assert the registration command and Server URL both equal the disposable env URL.

- [ ] **Step 2: Run the browser test and verify it fails against pre-change fixtures**

Extend `scripts/run-web-e2e.sh` to set
`TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE="$RUN_DIR/totp-master-key"`, retaining its existing unique
loopback port, temporary SQLite database, disposable admin Token, isolated config/state/runtime
directories, and exact cleanup trap. Then run:

```bash
scripts/run-web-e2e.sh
```

Expected: the harness chooses its own port and `/tmp/termflow-web-e2e.*` evidence directory; it
never uses the existing `8765` port, live token, or shared data volume.

Expected: FAIL on the old heading/QR or immediate-enable behavior while the existing `127.0.0.1:8765` service remains untouched.

- [ ] **Step 3: Update only disposable fixtures needed by the new behavior**

Make fixture TOTP helpers generate fresh counters for setup, enable, login, and disable. Do not reuse the live `.env`, Docker volume, admin token, or existing container. Wait for API responses and visible status changes rather than fixed sleeps.

- [ ] **Step 4: Run the isolated browser test and inspect screenshots/console**

Expected: browser test PASS; screenshots show full-width themes, the requested Server hierarchy, theme-colored QR dialog, onboarding, and toggle; no relevant console errors; reload preserves configured-disabled state.

- [ ] **Step 5: Run complete repository verification**

```bash
scripts/verify.sh
```

Expected: generated-contract check, all npm tests/typechecks/builds, all Python tests, Ruff, Mypy, Rust/Tauri checks, Compose validation, Docker build, and image-content verification all exit 0.

- [ ] **Step 6: Confirm current deployment stayed untouched and commit browser coverage**

```bash
docker ps --filter name=deploy-control-plane-1 --format '{{.Names}} {{.Status}} {{.Ports}}'
git status --short
git add apps/clients/web/e2e/settings-auth.spec.ts scripts/run-web-e2e.sh scripts/verify.sh
git commit -m "test(web): cover Settings and TOTP onboarding flow"
```

Expected: the original container is still healthy on its original loopback port; only exact disposable browser resources were removed.
