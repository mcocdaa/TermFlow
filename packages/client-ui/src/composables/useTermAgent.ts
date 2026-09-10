import { ApiError } from '@termflow/client-core'
import type { AgentConversationResponse, AgentSetupRequest, AgentSetupResponse } from '@termflow/client-contracts'
import { computed, onBeforeUnmount, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useClientRuntime } from '../runtime'
import type { PaneTopology } from '../types'
import type { AgentSetupSelection } from '../components/agent/AgentSetupForm.vue'
import { useSensitiveAuthorization } from './useSensitiveAuthorization'

export function agentReasonMessage(code: string | null | undefined): string {
  const messages: Record<string, string> = {
    stale_topology: '窗格布局已变化，请刷新后重新选择。',
    topology_unavailable: '窗格布局暂不可用，请连接终端后刷新。',
    disclosure_mismatch: '提供方披露已变化，请重新确认。',
    disclosure_required: '请确认当前提供方的数据发送说明。',
    binding_disclosure_stale: '提供方披露已变化，请重新确认。',
    binding_disclosure_required: '请确认当前提供方的数据发送说明。',
    provider_unavailable: '模型提供方暂不可用。',
    deployment_required: '请管理员完成 Agent 服务部署。',
    sensitive_action_reauthentication_required: '身份验证已失效，请重新操作。',
  }
  return messages[code ?? ''] ?? 'Agent 暂不可用，请稍后重试。'
}

export function useTermAgent(options: { termId: MaybeRefOrGetter<string>; requestedConversationId: MaybeRefOrGetter<string | null> }) {
  const runtime = useClientRuntime(), authorization = useSensitiveAuthorization()
  const setup = ref<AgentSetupResponse | null>(null), panes = ref<PaneTopology[]>([])
  const profiles = computed(() => setup.value?.profiles ?? [])
  const bindingId = computed(() => setup.value?.binding_id ?? null)
  const conversations = ref<AgentConversationResponse[]>([]), selectedConversationId = ref<string | null>(null)
  const pendingApprovalCount = ref(0), loading = ref(true), mutating = ref(false), error = ref('')
  let generation = 0, controller = new AbortController(), disposed = false
  let idempotencyKey: string | null = null
  let lastSelection: AgentSetupSelection | null = null
  let mutationController: AbortController | null = null
  const current = (g: number, term: string) => !disposed && g === generation && term === toValue(options.termId)

  function selectConversation(id: string | null) {
    if (id !== null && !conversations.value.some((entry) => entry.conversation_id === id && entry.binding_id === bindingId.value)) {
      selectedConversationId.value = null; error.value = '该会话不属于当前 Term。'; return
    }
    selectedConversationId.value = id
  }
  async function refresh() {
    controller.abort(); controller = new AbortController()
    const signal = controller.signal, g = ++generation, term = toValue(options.termId)
    loading.value = true; error.value = ''
    try {
      const [summary, topology] = await Promise.all([runtime.api.agents.getSetup(term, signal), runtime.api.terms.topology(term, signal)])
      if (!current(g, term) || summary.term_id !== term) return
      const list = summary.binding_id ? await runtime.api.agents.listConversations({ bindingId: summary.binding_id, signal }) : { conversations: [] }
      if (!current(g, term)) return
      setup.value = summary; panes.value = topology.topology.windows.flatMap((window) => window.panes)
      conversations.value = list.conversations.filter((entry) => entry.binding_id === summary.binding_id)
      selectConversation(toValue(options.requestedConversationId) ?? selectedConversationId.value ?? conversations.value[0]?.conversation_id ?? null)
      if (!error.value && summary.reason_code) error.value = agentReasonMessage(summary.reason_code)
    } catch (cause) {
      if (current(g, term) && !signal.aborted) error.value = agentReasonMessage(cause instanceof ApiError ? cause.code : null)
    } finally { if (current(g, term)) loading.value = false }
  }
  async function mutate(operation: (signal: AbortSignal) => Promise<unknown>, success?: () => void, refreshAfter = true) {
    if (mutating.value) return
    const term = toValue(options.termId), g = generation
    const ownController = new AbortController(); mutationController = ownController
    mutating.value = true; error.value = ''
    try {
      await authorization.run(() => {
        if (!current(g, term) || ownController.signal.aborted) throw new Error('sensitive_authorization_cancelled')
        return operation(ownController.signal)
      }, 'sensitive_action_reauthentication_required', ownController.signal)
      if (current(g, term)) {
        success?.()
        if (refreshAfter) await refresh()
      }
    } catch (cause) {
      if (current(g, term) && !ownController.signal.aborted && !(cause instanceof Error && cause.message === 'sensitive_authorization_cancelled')) error.value = agentReasonMessage(cause instanceof ApiError ? cause.code : null)
    } finally { if (mutationController === ownController) { mutationController = null; mutating.value = false } }
  }
  async function submitSetup(selection: AgentSetupSelection) {
    lastSelection = selection
    idempotencyKey ??= globalThis.crypto.randomUUID()
    const body: AgentSetupRequest = { term_id: toValue(options.termId), profile_id: selection.kind === 'existing' ? selection.profileId : null, profile_display_name: selection.kind === 'new' ? selection.profileDisplayName : null, pane_ids: [...selection.paneIds], topology_revision: selection.topologyRevision, disclosure_fingerprint: selection.disclosureFingerprint, accepted: true, idempotency_key: idempotencyKey }
    await mutate((signal) => runtime.api.agents.submitSetup(body, signal), () => { idempotencyKey = null; lastSelection = null })
  }
  async function activate() {
    const id = bindingId.value
    if (id) await mutate((signal) => runtime.api.agents.activate(id, signal))
  }
  async function acceptDisclosure() {
    const id = bindingId.value, disclosure = setup.value?.disclosure
    if (!id || !disclosure) return
    await mutate((signal) => runtime.api.agents.acceptDisclosure(id, {
      disclosure_fingerprint: disclosure.disclosure_fingerprint,
      accepted: true,
    }, signal))
  }
  async function createConversation() {
    const id = bindingId.value
    if (!id) return
    let created: AgentConversationResponse | null = null
    await mutate(async (signal) => {
      created = await runtime.api.agents.createConversation({ binding_id: id, title: null }, signal)
    }, () => {
      const conversation = created
      if (conversation === null || conversation.binding_id !== bindingId.value) return
      if (!conversations.value.some((entry) => entry.conversation_id === conversation.conversation_id)) conversations.value.push(conversation)
      selectConversation(conversation.conversation_id)
    }, false)
  }
  function resetForm() { idempotencyKey = null; lastSelection = null }
  async function retry() { if (lastSelection) await submitSetup(lastSelection); else await refresh() }
  watch(() => toValue(options.termId), () => {
    mutationController?.abort(); mutationController = null; mutating.value = false
    setup.value = null; panes.value = []; conversations.value = []; selectedConversationId.value = null; pendingApprovalCount.value = 0; resetForm(); void refresh()
  }, { immediate: true, flush: 'sync' })
  watch(() => toValue(options.requestedConversationId), (id) => { if (!loading.value) selectConversation(id) })
  onBeforeUnmount(() => { disposed = true; generation++; controller.abort(); mutationController?.abort() })
  return { setup, profiles, panes, bindingId, conversations, selectedConversationId, pendingApprovalCount, loading, mutating, error, refresh, submitSetup, activate, acceptDisclosure, selectConversation, createConversation, resetForm, retry }
}
