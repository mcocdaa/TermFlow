import type {
  OAuthAuthorizationDecisionResponse,
  OAuthAuthorizationPreviewResponse,
} from '@termflow/client-contracts'
import { ref } from 'vue'

type AuthorizationDecision = 'allow' | 'deny'

export interface DeviceAuthorizationApprovalApi {
  deviceAuthorizationPreview(userCode: string): Promise<OAuthAuthorizationPreviewResponse>
  decideAuthorization(input: {
    transactionId: string
    decision: AuthorizationDecision
    totpCode?: string
  }): Promise<OAuthAuthorizationDecisionResponse>
}

export interface DeviceAuthorizationApprovalOptions {
  onAuthenticationRequired?: () => Promise<void> | void
}

export type DeviceAuthorizationApprovalTerminalState = 'approved' | 'denied' | null

interface ApiErrorLike {
  kind?: unknown
  code?: unknown
}

function apiErrorKind(cause: unknown): string | undefined {
  if (typeof cause !== 'object' || cause === null) return undefined
  return typeof (cause as ApiErrorLike).kind === 'string' ? (cause as ApiErrorLike).kind as string : undefined
}

function apiErrorCode(cause: unknown): string | undefined {
  if (typeof cause !== 'object' || cause === null) return undefined
  return typeof (cause as ApiErrorLike).code === 'string' ? (cause as ApiErrorLike).code as string : undefined
}

export function normalizeDeviceAuthorizationCode(value: string): string {
  return value.trim().toUpperCase().replace(/\s+/g, '')
}

export function useDeviceAuthorizationApproval(
  api: DeviceAuthorizationApprovalApi,
  options: DeviceAuthorizationApprovalOptions = {},
) {
  const preview = ref<OAuthAuthorizationPreviewResponse | null>(null)
  const userCode = ref('')
  const totpCode = ref('')
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')
  const success = ref<DeviceAuthorizationApprovalTerminalState>(null)

  async function authenticationRequired(): Promise<boolean> {
    if (options.onAuthenticationRequired === undefined) return false
    await options.onAuthenticationRequired()
    return true
  }

  async function lookup(value = userCode.value): Promise<void> {
    const code = normalizeDeviceAuthorizationCode(value)
    if (!/^[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(code)) {
      error.value = '请输入格式为 ABCD-EFGH 的设备码。'
      return
    }

    userCode.value = code
    loading.value = true
    error.value = ''
    success.value = null
    try {
      preview.value = await api.deviceAuthorizationPreview(code)
    } catch (cause) {
      if (apiErrorKind(cause) === 'authentication' && await authenticationRequired()) return
      error.value = apiErrorCode(cause) === 'authorization_expired'
        ? '设备码无效或已过期。'
        : '无法查找设备码。'
    } finally {
      loading.value = false
    }
  }

  async function decide(decision: AuthorizationDecision): Promise<void> {
    if (preview.value === null || busy.value) return
    busy.value = true
    error.value = ''
    try {
      const result = await api.decideAuthorization({
        transactionId: preview.value.transaction_id,
        decision,
        ...(totpCode.value ? { totpCode: totpCode.value } : {}),
      })
      success.value = result.status
      totpCode.value = ''
    } catch (cause) {
      totpCode.value = ''
      if (apiErrorKind(cause) === 'authentication' && await authenticationRequired()) return
      error.value = apiErrorCode(cause) === 'authorization_expired'
        ? '设备码无效或已过期。'
        : '无法完成授权。'
    } finally {
      busy.value = false
    }
  }

  return { preview, userCode, totpCode, loading, busy, error, success, lookup, decide }
}
