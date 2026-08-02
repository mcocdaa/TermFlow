import { load } from '@tauri-apps/plugin-store'

const DEFAULT_ISSUER = 'http://127.0.0.1:8765'

function canonicalIssuer(value: string): string {
  const url = new URL(value)
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('server_url_invalid')
  if (url.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('https_required')
  return url.origin
}

function canonicalAuthorizeEndpoint(issuer: string, value: string): string {
  const canonical = canonicalIssuer(issuer)
  const endpoint = new URL(value)
  if (endpoint.origin !== canonical || endpoint.pathname !== '/api/v1/oauth/authorize' || endpoint.search || endpoint.hash) {
    throw new Error('authorization_endpoint_invalid')
  }
  return endpoint.toString()
}

class ServerConfig {
  current = DEFAULT_ISSUER
  async load() {
    const store = await load('settings.json', { autoSave: false, defaults: {} })
    const stored = await store.get<string>('issuer')
    if (stored !== undefined) this.current = canonicalIssuer(stored)
  }
  async replace(value: string) {
    this.current = canonicalIssuer(value)
    const store = await load('settings.json', { autoSave: false, defaults: {} })
    await store.set('issuer', this.current)
    await store.save()
  }
}

export const serverConfig = new ServerConfig()
export { canonicalAuthorizeEndpoint, canonicalIssuer }
