export type ApiErrorKind = 'offline' | 'authentication' | 'validation' | 'rate_limit' | 'server' | 'aborted' | 'http_capability_denied'

const safeMessagesZh: Record<ApiErrorKind, string> = {
  offline: '无法连接服务，请检查网络后重试。',
  authentication: '会话已过期，请重新登录。',
  validation: '提交的内容不符合要求。',
  rate_limit: '操作过于频繁，请稍后重试。',
  server: '服务暂时不可用，请稍后重试。',
  aborted: '请求已取消。',
  http_capability_denied: '客户端网络权限配置无效，请升级或重新安装 TermFlow。',
}

const safeMessagesEn: Record<ApiErrorKind, string> = {
  offline: 'Unable to connect to the server. Please check your network and try again.',
  authentication: 'Session expired. Please sign in again.',
  validation: 'The submitted content is invalid.',
  rate_limit: 'Too many requests. Please try again later.',
  server: 'Service is temporarily unavailable. Please try again later.',
  aborted: 'Request canceled.',
  http_capability_denied: 'Client network capability configuration is invalid. Please update or reinstall TermFlow.',
}

export type ApiErrorLocale = 'zh' | 'en'

let currentApiErrorLocale: ApiErrorLocale = 'zh'

export function setApiErrorLocale(locale: ApiErrorLocale): void {
  currentApiErrorLocale = locale
}

export function getApiErrorLocale(): ApiErrorLocale {
  return currentApiErrorLocale
}

export function safeMessageFor(kind: ApiErrorKind): string {
  return currentApiErrorLocale === 'en' ? safeMessagesEn[kind] : safeMessagesZh[kind]
}

export interface ApiErrorDetails {
  status?: number
  code?: string
  requestId?: string
  retryAfterSeconds?: number
  dpopNonce?: string
}

export class ApiError extends Error {
  readonly status?: number
  readonly code?: string
  readonly requestId?: string
  readonly retryAfterSeconds?: number
  readonly dpopNonce?: string

  constructor(public readonly kind: ApiErrorKind, details: ApiErrorDetails = {}) {
    super(safeMessageFor(kind))
    this.name = 'ApiError'
    if (details.status !== undefined) this.status = details.status
    if (details.code !== undefined) this.code = details.code
    if (details.requestId !== undefined) this.requestId = details.requestId
    if (details.retryAfterSeconds !== undefined) this.retryAfterSeconds = details.retryAfterSeconds
    if (details.dpopNonce !== undefined) this.dpopNonce = details.dpopNonce
  }
}
