# Tauri HTTP IPv6 Scope Repair Design

## Goal

Restore native-client access to configured relay servers by making every Tauri HTTP capability rule valid, while retaining loopback IPv6 support and exposing capability failures separately from genuine network outages.

## Root cause

The native capability currently contains `http://[::1]:*`. `tauri-plugin-http` parses each allow entry as a `urlpattern` pattern before matching any request. Raw IPv6 colons are pattern metacharacters, so this entry fails deserialization. Tauri aborts construction of the complete HTTP scope, and valid entries such as `http://127.0.0.1:*` never reach matching.

The client transport then converts every non-abort exception into `offline`, which makes this local capability defect appear to be a server or network failure.

## Chosen approach

- Replace the malformed entry with JSON `http://[\\:\\:1]:*`, which delivers `http://[\:\:1]:*` to the URLPattern parser. This follows the IPv6 escaping used by the pinned `urlpattern` implementation and preserves IPv6 loopback support.
- Keep the existing security boundary: arbitrary relay servers require HTTPS; plaintext HTTP remains limited to `127.0.0.1`, `localhost`, and `[::1]` for local development.
- Classify Tauri capability/scope failures as `http_capability_denied` instead of `offline`. Do not expose an unrestricted remote HTTP fallback.

## Alternatives rejected

- Removing IPv6 is simpler but conflicts with the client issuer validator, which already accepts IPv6 loopback URLs.
- Allowing `http://*` would hide the malformed pattern but weaken transport security for credentials and authorization codes.
- Ignoring malformed entries would make security policy dependent on silent parser recovery; Tauri correctly fails closed instead.

## Testing

- Add a native capability contract test that uses the actual pinned URLPattern parser, not only JSON/schema checks.
- Assert that every configured HTTP allow entry parses successfully.
- Assert matching for HTTPS relay URLs and loopback HTTP via IPv4, `localhost`, and IPv6, while rejecting non-loopback HTTP.
- Add transport tests proving scope/capability errors map to `http_capability_denied` and ordinary fetch failures remain `offline`.
- Run focused native-client tests, the full Tauri frontend test suite, type checking, Rust checks, and capability/config validation.

## Delivery boundary

The repair changes only the native client. Docker, the control plane, Web C, relay-server configuration, and deployed ports do not change. Existing Windows installations require a newly built installer; local verification does not by itself prove a GitHub Actions package was produced.
