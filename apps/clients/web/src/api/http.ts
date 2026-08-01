export type ApiErrorKind = 'offline' | 'authentication' | 'validation' | 'rate_limit' | 'server' | 'aborted'

const safeMessages: Record<ApiErrorKind, string> = {
  offline: '无法连接服务，请检查网络后重试。',
  authentication: '会话已过期，请重新登录。',
  validation: '提交的内容不符合要求。',
  rate_limit: '操作过于频繁，请稍后重试。',
  server: '服务暂时不可用，请稍后重试。',
  aborted: '请求已取消。',
}

export class ApiError extends Error {
  constructor(public readonly kind: ApiErrorKind, public readonly status?: number, public readonly code?: string) {
    super(safeMessages[kind])
    this.name = 'ApiError'
  }
}

export interface ApiRequestOptions extends Omit<RequestInit, 'body' | 'credentials'> {
  body?: unknown
}

function errorKind(status: number): ApiErrorKind {
  if (status === 401 || status === 403) return 'authentication'
  if (status === 400 || status === 404 || status === 409 || status === 422) return 'validation'
  if (status === 429) return 'rate_limit'
  return 'server'
}

export async function apiRequest<T = void>(path: `/${string}`, options: ApiRequestOptions = {}): Promise<T> {
  if (path.startsWith('//') || path.includes('://')) throw new ApiError('validation')
  const headers = new Headers(options.headers)
  headers.set('accept', 'application/json')
  let body: BodyInit | undefined
  if (options.body !== undefined) {
    headers.set('content-type', 'application/json')
    body = JSON.stringify(options.body)
  }
  try {
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      body,
      headers,
      credentials: 'same-origin',
    })
    if (!response.ok) {
      let code: string | undefined
      try {
        const payload = await response.json() as { error?: { code?: string } }
        code = payload.error?.code
      } catch { /* the UI deliberately ignores raw response text */ }
      throw new ApiError(errorKind(response.status), response.status, code)
    }
    if (response.status === 204) return undefined as T
    const contentType = response.headers.get('content-type') ?? ''
    return contentType.includes('application/json') ? await response.json() as T : undefined as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (error instanceof DOMException && error.name === 'AbortError') throw new ApiError('aborted')
    throw new ApiError('offline')
  }
}
