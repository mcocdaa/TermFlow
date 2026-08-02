import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { createMemoryAccessVault } from './adapters/memoryAccessVault'
import { canonicalAuthorizeEndpoint, canonicalIssuer } from './serverConfig'

describe('Tauri security composition', () => {
  it('keeps short-lived access in memory and exposes no browser credential storage', async () => {
    const vault = createMemoryAccessVault()
    await vault.replace('https://b.example', { accessToken: 'short-lived', expiresAt: '2026-08-02T12:00:00Z', tokenType: 'DPoP' })
    await expect(vault.load('https://b.example')).resolves.toMatchObject({ accessToken: 'short-lived' })
    await vault.clear('https://b.example')
    await expect(vault.load('https://b.example')).resolves.toBeNull()

    const source = ['runtime.ts', 'nativeAuth.ts', 'adapters/memoryAccessVault.ts'].map((file) => readFileSync(resolve(import.meta.dirname, file), 'utf8')).join('\n')
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB/i)
    expect(source).not.toMatch(/refreshToken\s*:/)
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
    expect(rust).toContain('struct AccessCredential')
    const accessShape = rust.slice(rust.indexOf('struct AccessCredential'), rust.indexOf('struct AccessState'))
    expect(accessShape).not.toContain('refresh')
    expect(rust).not.toContain('println!')
    expect(rust).not.toContain('dbg!')
    expect(shell.indexOf('tauri_plugin_single_instance::init')).toBeLessThan(shell.indexOf('tauri_plugin_deep_link::init'))
    expect(shell).not.toContain('println!')
  })
})
