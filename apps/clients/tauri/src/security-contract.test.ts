import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { canonicalAuthorizeEndpoint, canonicalIssuer } from './serverConfig'

describe('Tauri security composition', () => {
  it('uses the Windows GUI subsystem for packaged release builds', () => {
    const entrypoint = readFileSync(resolve(import.meta.dirname, '../src-tauri/src/main.rs'), 'utf8')
    expect(entrypoint).toContain('#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]')
  })

  it('keeps credentials entirely outside the WebView source tree', () => {
    const source = ['runtime.ts', 'nativeAuth.ts'].map((file) => readFileSync(resolve(import.meta.dirname, file), 'utf8')).join('\n')
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB/i)
    expect(source).not.toMatch(/refreshToken\s*:/)
    expect(source).not.toMatch(/CredentialVault|NativeAccessCredential|accessToken/)
    expect(existsSync(resolve(import.meta.dirname, 'adapters/memoryAccessVault.ts'))).toBe(false)
    expect(existsSync(resolve(import.meta.dirname, 'adapters/tauriCredentialVault.ts'))).toBe(false)
  })

  it('reports the logical package version independently of platform bundle versions', () => {
    const source = readFileSync(resolve(import.meta.dirname, 'nativeAuth.ts'), 'utf8')
    expect(source).toContain("from './buildVersion'")
    expect(source).toContain('version: buildVersion')
    expect(source).not.toContain('getVersion')
  })

  it('accepts HTTPS or explicit loopback only and strips no ambiguous URL parts', () => {
    expect(canonicalIssuer('https://b.example/')).toBe('https://b.example')
    expect(canonicalIssuer('http://127.0.0.1:8765/')).toBe('http://127.0.0.1:8765')
    expect(canonicalIssuer('http://[::1]:8765/')).toBe('http://[::1]:8765')
    expect(() => canonicalIssuer('http://public.example')).toThrow('https_required')
    expect(() => canonicalIssuer('https://b.example/path')).toThrow('server_url_invalid')
    expect(canonicalAuthorizeEndpoint('https://b.example', 'https://b.example/api/v1/oauth/authorize')).toBe('https://b.example/api/v1/oauth/authorize')
    expect(() => canonicalAuthorizeEndpoint('https://b.example', 'https://attacker.example/api/v1/oauth/authorize')).toThrow('authorization_endpoint_invalid')
    expect(() => canonicalAuthorizeEndpoint('https://b.example', 'https://b.example/api/v1/oauth/authorize?redirect=https://attacker.example')).toThrow('authorization_endpoint_invalid')
  })

  it('uses shared UI/core and narrow Rust commands without returning refresh material', () => {
    const main = readFileSync(resolve(import.meta.dirname, 'main.ts'), 'utf8')
    const rust = readFileSync(resolve(import.meta.dirname, '../src-tauri/src/auth.rs'), 'utf8')
    const shell = readFileSync(resolve(import.meta.dirname, '../src-tauri/src/lib.rs'), 'utf8')
    expect(main).toContain('@termflow/client-ui')
    expect(rust).toContain('struct NativeAuthorizationStatus')
    const statusShape = rust.slice(rust.indexOf('struct NativeAuthorizationStatus'), rust.indexOf('struct AccessState'))
    expect(statusShape).not.toContain('access_token')
    const deviceExchange = rust.slice(rust.indexOf('pub async fn native_exchange_device_code'), rust.indexOf('pub fn native_clear_credentials'))
    expect(deviceExchange).toContain('Result<NativeAuthorizationStatus, String>')
    expect(deviceExchange).not.toContain('TokenResponse, String')
    expect(rust).not.toContain('println!')
    expect(rust).not.toContain('dbg!')
    expect(shell.indexOf('tauri_plugin_single_instance::init')).toBeLessThan(shell.indexOf('tauri_plugin_deep_link::init'))
    expect(shell).not.toContain('println!')
  })

  it('clears native refresh credentials only through a controlled Rust command', () => {
    const rust = readFileSync(resolve(import.meta.dirname, '../src-tauri/src/auth.rs'), 'utf8')
    const shell = readFileSync(resolve(import.meta.dirname, '../src-tauri/src/lib.rs'), 'utf8')
    expect(rust).toContain('pub fn native_clear_credentials')
    expect(rust).toContain('let issuer = canonical_issuer(&issuer)?')
    expect(rust).toContain('clear_native_credentials(&state, &issuer)')
    expect(shell).toContain('auth::native_clear_credentials')
  })
})
