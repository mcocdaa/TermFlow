import { describe, expect, it } from 'vitest'
import { canonicalAuthorizeEndpoint, canonicalIssuer } from './serverConfig'

describe('Tauri security composition', () => {
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
})
