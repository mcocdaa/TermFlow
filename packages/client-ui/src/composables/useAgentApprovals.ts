//: Approval list/detail/decide/revoke composable (M6b spec §4.7). The B
//: approval state machine is the source of truth; every mutating action
//: refreshes the list on conflict/expiry (409/410) and drops the entry on
//: 404, per the spec's error mapping. `busyIds` drives the aria-busy state
//: of decision buttons while a request is in flight (M6b spec §6.5).
import { createApprovalsApi, ApiError } from '@termflow/client-core'
import type { ApprovalResponse } from '@termflow/client-contracts'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from './useBottomToast'
import { useSession } from './useSession'
import { useSensitiveAuthorization } from './useSensitiveAuthorization'

export interface UseAgentApprovalsOptions {
  /** Restrict the list to one conversation (conversation-scoped panel). */
  conversationId?: string
}

//: Shared approval entry cache (approval_id → latest known REST entry).
//: List loads seed it with every entry (the list already carries all the
//: fields the timeline approval card renders), so the cards reuse the
//: panel's data instead of firing one detail request per card. approval_id
//: values are B-generated globally, so the cache is safe across
//: conversations. Genuine misses are fetched once and cached.
const approvalEntryCache = new Map<string, ApprovalResponse>()

//: In-flight list-load tracking. A card mounting while the panel's list is
//: still loading waits for it to settle and re-checks the cache before
//: falling back to its own detail fetch (avoids the N-fetch flood on the
//: first render and on live permission arrivals).
let pendingListLoads = 0
const listSettledWaiters: Array<() => void> = []

function beginApprovalListLoad() {
  pendingListLoads += 1
}

function endApprovalListLoad() {
  pendingListLoads -= 1
  if (pendingListLoads === 0) {
    const waiters = listSettledWaiters.splice(0)
    for (const resolve of waiters) resolve()
  }
}

/** Seed the shared cache from a list response (all states, not just pending). */
function seedApprovalEntryCache(approvals: ApprovalResponse[]) {
  for (const approval of approvals) approvalEntryCache.set(approval.approval_id, approval)
}

/** A card-side detail fetch result enters the shared cache. */
export function cacheApprovalDetail(entry: ApprovalResponse): void {
  approvalEntryCache.set(entry.approval_id, entry)
}

export function cachedApprovalEntry(approvalId: string): ApprovalResponse | undefined {
  return approvalEntryCache.get(approvalId)
}

export function hasPendingApprovalListLoads(): boolean {
  return pendingListLoads > 0
}

export function whenApprovalListLoadsSettled(): Promise<void> {
  if (pendingListLoads === 0) return Promise.resolve()
  return new Promise((resolve) => listSettledWaiters.push(resolve))
}

/** Test support: reset the module-level cache (and settlement state) between tests. */
export function resetApprovalEntryCache(): void {
  approvalEntryCache.clear()
  pendingListLoads = 0
  listSettledWaiters.length = 0
}

export function useAgentApprovals(options: UseAgentApprovalsOptions = {}) {
  const runtime = useClientRuntime()
  const authorization = useSensitiveAuthorization()
  const toast = useBottomToast()
  const { clearSessionState } = useSession()
  const router = useRouter()
  const route = useRoute()
  const approvals = ref<ApprovalResponse[]>([])
  const loading = ref(true)
  const busyIds = ref<ReadonlySet<string>>(new Set())
  const approvalsApi = createApprovalsApi(runtime.api.request)
  let controller: AbortController | null = null
  let disposed = false
  let loadGeneration = 0

  /** Same handling as the stream path (useAgentConversation 4401). */
  function handleAuthenticationRequired() {
    clearSessionState()
    void router.replace({ path: '/login', query: { redirect: route.fullPath } })
  }

  async function load() {
    // Generation guard: a slow mount-time load must not overwrite a newer
    // list refreshed after decide()/revoke() succeeded (it would briefly
    // resurrect already-handled approvals). Only the latest load applies.
    const generation = ++loadGeneration
    loading.value = true
    beginApprovalListLoad()
    try {
      const response = await approvalsApi.list({
        ...(options.conversationId !== undefined ? { conversationId: options.conversationId } : {}),
        ...(controller !== null ? { signal: controller.signal } : {}),
      })
      // Every entry (any state) feeds the shared card cache.
      seedApprovalEntryCache(response.approvals)
      if (!disposed && generation === loadGeneration) approvals.value = response.approvals
    } catch (error) {
      if (generation === loadGeneration && !(error instanceof ApiError && error.kind === 'aborted')) {
        toast.show({ text: '无法加载审批列表。', tone: 'error' })
      }
    } finally {
      endApprovalListLoad()
      if (!disposed && generation === loadGeneration) loading.value = false
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
    if (error instanceof Error && error.message === 'sensitive_authorization_cancelled') return
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
      // The session is stale; a toast alone would leave the user stuck on
      // a session that can never decide again. Align with the stream path:
      // clear the session and redirect to login.
      handleAuthenticationRequired()
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
    toast.show({ text: error.status === 428 ? '身份验证已失效，请重新操作。' : '操作失败，请稍后重试。', tone: 'error' })
  }

  async function decide(approvalId: string, decision: 'approve' | 'deny') {
    if (isBusy(approvalId)) return
    setBusy(approvalId, true)
    try {
      const signal = controller?.signal
      const operation = () => approvalsApi.decide(approvalId, decision, signal)
      if (decision === 'approve') await authorization.run(operation, 'approval_reauthentication_required', signal)
      else await operation()
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
