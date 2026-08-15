//: Conversation list/create/delete plus per-conversation pending approval
//: badges (M6b spec §4.7). Deleting a conversation clears its persisted
//: cursor (M6b spec §4.4: cursor 清除时机 = 会话删除成功). The list is
//: scoped by binding (B requires `binding_id`).
import type { AgentConversationResponse } from '@termflow/client-contracts'
import { createApprovalsApi } from '@termflow/client-core'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from './useBottomToast'

export function useAgentConversations(bindingId?: string) {
  const runtime = useClientRuntime()
  const toast = useBottomToast()
  const conversations = ref<AgentConversationResponse[]>([])
  /** conversation_id → pending approval count (badge source). */
  const pendingCounts = ref<ReadonlyMap<string, number>>(new Map())
  const loading = ref(true)
  const approvalsApi = createApprovalsApi(runtime.api.request)
  let controller: AbortController | null = null
  let disposed = false

  async function load() {
    loading.value = true
    try {
      const [list, approvals] = await Promise.all([
        runtime.api.agents.listConversations({
          ...(bindingId !== undefined ? { bindingId } : {}),
          ...(controller !== null ? { signal: controller.signal } : {}),
        }),
        approvalsApi.list({
          ...(controller !== null ? { signal: controller.signal } : {}),
        }),
      ])
      if (disposed) return
      conversations.value = list.conversations
      const counts = new Map<string, number>()
      for (const approval of approvals.approvals) {
        if (approval.state !== 'pending') continue
        counts.set(approval.conversation_id, (counts.get(approval.conversation_id) ?? 0) + 1)
      }
      pendingCounts.value = counts
    } catch (error) {
      if (!disposed) toast.show({ text: '无法加载会话列表。', tone: 'error' })
    } finally {
      if (!disposed) loading.value = false
    }
  }

  async function create(binding: string, title?: string) {
    try {
      const created = await runtime.api.agents.createConversation({ binding_id: binding, title: title ?? null })
      conversations.value = [created, ...conversations.value]
      toast.show({ text: '会话已创建。', tone: 'success' })
      return created
    } catch (error) {
      toast.show({ text: '创建会话失败。', tone: 'error' })
      return null
    }
  }

  async function remove(conversationId: string) {
    try {
      await runtime.api.agents.deleteConversation(conversationId, controller?.signal)
      conversations.value = conversations.value.filter((conversation) => conversation.conversation_id !== conversationId)
      // Deleted conversations can never resume (M6b spec §4.4).
      runtime.agentCursorStore.clear(conversationId)
      toast.show({ text: '会话已删除。', tone: 'success' })
    } catch (error) {
      toast.show({ text: '删除会话失败。', tone: 'error' })
    }
  }

  function pendingCount(conversationId: string): number {
    return pendingCounts.value.get(conversationId) ?? 0
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

  return { conversations, pendingCounts, loading, refresh: load, create, remove, pendingCount }
}
