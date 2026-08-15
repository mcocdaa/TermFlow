//: Approval list/detail/decide/revoke composable (M6b spec §4.7). The B
//: approval state machine is the source of truth; every mutating action
//: refreshes the list on conflict/expiry (409/410) and drops the entry on
//: 404, per the spec's error mapping. `busyIds` drives the aria-busy state
//: of decision buttons while a request is in flight (M6b spec §6.5).
import { createApprovalsApi, ApiError } from '@termflow/client-core'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from './useBottomToast'

export interface UseAgentApprovalsOptions {
  /** Restrict the list to one conversation (conversation-scoped panel). */
  conversationId?: string
}

export function useAgentApprovals(options: UseAgentApprovalsOptions = {}) {
  const runtime = useClientRuntime()
  const toast = useBottomToast()
  const approvals = ref<ApprovalResponse[]>([])
  const loading = ref(true)
  const busyIds = ref<ReadonlySet<string>>(new Set())
  const approvalsApi = createApprovalsApi(runtime.api.request)
  let controller: AbortController | null = null
  let disposed = false

  async function load() {
    loading.value = true
    try {
      const response = await approvalsApi.list({
        ...(options.conversationId !== undefined ? { conversationId: options.conversationId } : {}),
        ...(controller !== null ? { signal: controller.signal } : {}),
      })
      if (!disposed) approvals.value = response.approvals
    } catch (error) {
      if (!(error instanceof ApiError && error.kind === 'aborted')) {
        toast.show({ text: '无法加载审批列表。', tone: 'error' })
      }
    } finally {
      if (!disposed) loading.value = false
    }
  }

  function setBusy(approvalId: string, busy: boolean) {
    const next = new Set(busyIds.value)
    if (busy) next.add(approvalId)
    else next.delete(approvalId)
    busyIds.value = next
  }

  function isBusy(approvalId: string): boolean {
    return busyIds.value.has(approvalId)
  }

  /** 409/410 → refresh + toast; 404 → drop the entry; auth epoch → re-login. */
  async function handleError(error: unknown, approvalId: string) {
    if (!(error instanceof ApiError)) {
      toast.show({ text: '操作失败，请稍后重试。', tone: 'error' })
      return
    }
    if (error.kind === 'aborted') return
    if (error.status === 404 || error.code === 'approval_not_found') {
      approvals.value = approvals.value.filter((approval) => approval.approval_id !== approvalId)
      toast.show({ text: '该审批请求已不存在。', tone: 'error' })
      return
    }
    if (error.status === 409 && error.code === 'approval_auth_epoch_stale') {
      toast.show({ text: '会话凭据已变更，请重新登录后再处理审批。', tone: 'error' })
      return
    }
    if (error.status === 409 || error.status === 410) {
      toast.show({
        text: error.status === 410 ? '该审批请求已过期。' : '审批状态已变化，列表已刷新。',
        tone: 'error',
      })
      await load()
      return
    }
    toast.show({ text: error.message, tone: 'error' })
  }

  async function decide(approvalId: string, decision: 'approve' | 'deny') {
    if (isBusy(approvalId)) return
    setBusy(approvalId, true)
    try {
      await approvalsApi.decide(approvalId, decision, controller?.signal)
      toast.show({ text: decision === 'approve' ? '已批准。' : '已拒绝。', tone: 'success' })
      await load()
    } catch (error) {
      await handleError(error, approvalId)
    } finally {
      setBusy(approvalId, false)
    }
  }

  async function revoke(approvalId: string) {
    if (isBusy(approvalId)) return
    setBusy(approvalId, true)
    try {
      await approvalsApi.revoke(approvalId, controller?.signal)
      toast.show({ text: '已撤销审批。', tone: 'success' })
      await load()
    } catch (error) {
      await handleError(error, approvalId)
    } finally {
      setBusy(approvalId, false)
    }
  }

  onMounted(() => {
    controller = new AbortController()
    void load()
  })
  onBeforeUnmount(() => {
    disposed = true
    controller?.abort()
    controller = null
  })

  return { approvals, loading, busyIds, isBusy, refresh: load, decide, revoke }
}
