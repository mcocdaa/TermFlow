# Settings and TOTP Wizard Polish Design

**Date:** 2026-08-03

**Status:** Approved in conversation

## Goal

Make the relay server field read like a normal form field, make the public reverse-proxy URL discoverable from the root Compose environment example, and replace the visually ambiguous five-item TOTP guide with a clear three-step wizard whose active task is centered inside the page.

## Scope

This change is limited to shared client UI, Web C browser coverage, deployment examples, and the local ignored Compose environment file. It does not change TOTP APIs, authentication semantics, terminal behavior, reverse-proxy configuration, or persisted data.

## Server URL field

`ServerConnectionPanel` will use the same field hierarchy as the computer-name form:

- `服务网址` uses the normal field-label typography, not a section heading. Because the value remains a read-only code block rather than an editable input, the label text and value row are associated with `aria-labelledby` instead of using an invalid native `label[for]` target.
- The QR trigger remains beside the label and keeps its accessible name.
- The URL value and copy action remain in the row immediately below the label.
- Field spacing follows the existing `label + control` rhythm used by `EnrollmentDialog`; the label does not receive a heading-sized font or an artificial minimum-height row.

The panel-level `Server / 中继服务器` heading remains unchanged.

## Compose environment example

The repository-root `.env.example` becomes the canonical Compose environment example because documented Compose commands use `--env-file .env` from the repository root. It includes:

- `TERMFLOW_PUBLIC_BASE_URL`, documented as the externally reachable HTTPS origin supplied by the reverse proxy in production;
- `TERMFLOW_TRUSTED_WEB_ORIGINS`, normally matching that origin;
- the existing administrator token, host port, session, enrollment, and terminal limits.

The old `deploy/env.example` path is removed to avoid two independently editable copies, and documentation links are updated to `.env.example`. The ignored local `.env` receives `TERMFLOW_PUBLIC_BASE_URL` when it is missing; its administrator token is never copied into tracked files.

## Three-step TOTP guide

The visible guide is reduced to three user-meaningful stages:

1. `验证身份`
2. `绑定验证器`
3. `启用登录保护`

Each stage exposes one of three visual states:

- `complete`: accent-colored check mark and completed text;
- `current`: filled accent marker, emphasized label, and `aria-current="step"`;
- `upcoming`: muted marker and label.

State mapping is deterministic:

- Initial setup or reconfiguration credentials form: step 1 is current.
- A pending setup containing the provisioning URI: step 1 is complete and step 2 is current.
- A bound authenticator with login protection disabled: steps 1 and 2 are complete and step 3 is current.
- Login protection enabled: all three steps are complete.

Intermediate backend actions such as confirming and saving the binding remain part of step 2 rather than appearing as separate user-facing stages.

## TOTP card layout

Every wizard state has a card header with a left-aligned task title and a compact right-aligned progress label such as `第 2 步，共 3 步`. The card body is constrained and centered instead of stretching content from the far-left edge.

For `绑定验证器` on desktop:

- A centered setup group uses two columns.
- The themed QR code occupies the left column and is centered within that column.
- The verification-code label, six-digit input, error state, and `确认绑定` action occupy the right column.
- The setup key is hidden by default in a semantic disclosure labeled `无法扫描？使用设置密钥`; opening it reveals the key and a copy action.

On narrow screens the setup group becomes a single column with the QR code above the verification form. The three-step guide remains horizontal so the user can understand overall progress without a tall five-row list.

The identity form and login-protection setting use the same card header and centered body width. Reconfiguration returns to step 1. Existing API calls and the separate confirmation dialog for enabling or disabling login protection remain unchanged.

The structure follows the common TOTP sequence documented by GitHub: scan a provisioning QR code, use a setup key only when scanning is unavailable, enter the generated six-digit code, and then save the method.

## Accessibility

- The guide is an ordered list with an accessible label.
- Only the current stage receives `aria-current="step"`.
- Completed markers include a visual check mark without replacing the stage text.
- The setup-key disclosure is keyboard-operable and closed by default.
- Labels remain programmatically associated with the server value group and TOTP input.
- Existing focus return, error alerts, and button disabled states remain intact.

## Testing

Unit coverage will assert:

- the server field uses a label rather than an `h3`;
- the three TOTP stages and their `complete/current/upcoming` states;
- `aria-current` follows each state transition;
- the binding card exposes the new header, centered setup group, and closed setup-key disclosure;
- setup-key copying uses the runtime clipboard;
- existing setup, confirmation, protection, and unavailable-state behavior remains intact.

Browser coverage will assert desktop geometry, mobile stacking, current-step visibility, absence of outer-page overflow, and theme-colored QR rendering. Typecheck, shared UI tests, Web C build, and an isolated authenticated browser run are required before deployment.

## Reference

- GitHub Docs, “Configuring two-factor authentication”: https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/configuring-two-factor-authentication
