export type ApiErrorKind = 'offline' | 'authentication' | 'validation' | 'rate_limit' | 'server' | 'aborted'

const safeMessages: Record<ApiErrorKind, string> = {
  offline: '无法连接服务，请检查网络后重试。',
  authentication: '会话已过期，请重新登录。',
  validation: '提交的内容不符合要求。',
  rate_limit: '操作过于频繁，请稍后重试。',
  server: '服务暂时不可用，请稍后重试。',
  aborted: '请求已取消。',
}

export interface ApiErrorDetails {
  status?: number
  code?: string
  requestId?: string
}

export class ApiError extends Error {
  readonly status?: number
  readonly code?: string
  readonly requestId?: string

  constructor(public readonly kind: ApiErrorKind, details: ApiErrorDetails = {}) {
    super(safeMessages[kind])
    this.name = 'ApiError'
    if (details.status !== undefined) this.status = details.status
    if (details.code !== undefined) this.code = details.code
    if (details.requestId !== undefined) this.requestId = details.requestId
  }
}
