//: Conversation list/create/delete plus per-conversation pending approval
//: badges (M6b spec §4.7). Deleting a conversation clears its persisted
//: cursor (M6b spec §4.4: cursor 清除时机 = 会话删除成功). The list is
//: scoped by binding (B requires `binding_id`); the binding may be a plain
//: value or a reactive source — the AgentView selector re-scopes the list
//: by switching the ref, and a missing binding skips the fetch entirely.
import type { AgentConversationResponse } from '@termflow/client-contracts'
import { ApiError, createApprovalsApi } from '@termflow/client-core'
import { onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from './useBottomToast'

export function useAgentConversations(bindingId?: MaybeRefOrGetter<string | undefined>) {
  const runtime = useClientRuntime()
  const toast = useBottomToast()
  const conversations = ref<AgentConversationResponse[]>([])
  /** conversation_id → pending approval count (badge source). */
  const pendingCounts = ref<ReadonlyMap<string, number>>(new Map())
  const loading = ref(true)
  const approvalsApi = createApprovalsApi(runtime.api.request)
  // List loads and mutations keep separate controllers: load() aborts its
  // own in-flight list request on a binding switch, which must not cancel
  // an in-flight delete/create (the server may have completed it while the
  // UI would report a failure).
  let listController: AbortController | null = null
  let mutationController: AbortController | null = null
  let loadGeneration = 0
  let disposed = false

  async function load() {
    // Abort a still-in-flight load first: a rapid binding switch must not
    // let the older scope resolve last and overwrite the newer list.
    listController?.abort()
    listController = new AbortController()
    const generation = ++loadGeneration
    const current = toValue(bindingId)
    loading.value = true
    if (current === undefined) {
      // No binding selected yet: B requires the scope, nothing to list.
      conversations.value = []
      pendingCounts.value = new Map()
      loading.value = false
      return
    }
    // Drop the previous binding's rows up front so a binding switch never
    // shows a stale list while the new scope is in flight.
    conversations.value = []
    pendingCounts.value = new Map()
    try {
      const [list, approvals] = await Promise.all([
        runtime.api.agents.listConversations({
          bindingId: current,
          signal: listController.signal,
        }),
        approvalsApi.list({
          signal: listController.signal,
        }),
      ])
      if (disposed || generation !== loadGeneration) return
      conversations.value = list.conversations
      const counts = new Map<string, number>()
      for (const approval of approvals.approvals) {
        if (approval.state !== 'pending') continue
        counts.set(approval.conversation_id, (counts.get(approval.conversation_id) ?? 0) + 1)
      }
      pendingCounts.value = counts
    } catch (error) {
      // Aborted = superseded by a newer load or unmount; not a failure.
      if (!disposed && !(error instanceof ApiError && error.kind === 'aborted')) {
        toast.show({ text: '无法加载会话列表。', tone: 'error' })
      }
    } finally {
      if (!disposed && generation === loadGeneration) loading.value = false
    }
  }

  async function create(binding: string, title?: string) {
    try {
      const created = await runtime.api.agents.createConversation(
        { binding_id: binding, title: title ?? null },
        mutationController?.signal,
      )
      conversations.value = [created, ...conversations.value]
      toast.show({ text: '会话已创建。', tone: 'success' })
      return created
    } catch (error) {
      // An unmount abort is not a user-visible failure (mirrors the list
      // load's aborted filter); the caller still sees null and keeps the
      // typed title for a retry.
      if (error instanceof ApiError && error.kind === 'aborted') return null
      toast.show({ text: '创建会话失败。', tone: 'error' })
      return null
    }
  }

  async function remove(conversationId: string) {
    try {
      await runtime.api.agents.deleteConversation(conversationId, mutationController?.signal)
      conversations.value = conversations.value.filter((conversation) => conversation.conversation_id !== conversationId)
      // Deleted conversations can never resume (M6b spec §4.4).
      runtime.agentCursorStore.clear(conversationId)
      toast.show({ text: '会话已删除。', tone: 'success' })
    } catch (error) {
      // An unmount abort is not a user-visible failure (mirrors the list
      // load's aborted filter).
      if (error instanceof ApiError && error.kind === 'aborted') return
      toast.show({ text: '删除会话失败。', tone: 'error' })
    }
  }

  function pendingCount(conversationId: string): number {
    return pendingCounts.value.get(conversationId) ?? 0
  }

  onMounted(() => {
    mutationController = new AbortController()
    void load()
  })
  // A switched binding re-scopes the list (and the approval badges).
  watch(() => toValue(bindingId), () => {
    void load()
  })
  onBeforeUnmount(() => {
    disposed = true
    listController?.abort()
    listController = null
    mutationController?.abort()
    mutationController = null
  })

  return { conversations, pendingCounts, loading, refresh: load, create, remove, pendingCount }
}
