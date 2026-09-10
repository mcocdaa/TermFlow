import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { expect, it, vi } from 'vitest'
import { ApiError } from '@termflow/client-core'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import { useTermAgent } from './useTermAgent'
import type { AgentSetupResponse } from '@termflow/client-contracts'

export const setupResponse = (term = 't1'): AgentSetupResponse => ({ state: 'unconfigured', term_id: term, binding_id: null, profile: null, profiles: [], token: { installed: false, expires_at: null }, runtime: null, pane_policy: null, disclosure: null, topology_revision: 1, reason_code: null })
function harness(getSetup: ReturnType<typeof vi.fn>, authorizeNative = vi.fn(async () => 'authenticated' as const)) {
  const runtime = createFakeRuntime({ sensitiveAuthorization: { mode: 'native-oauth', authorizeNative } })
  runtime.api.agents.getSetup = getSetup
  runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [] }))
  const term = ref('t1'), requested = ref<string | null>(null)
  let state!: ReturnType<typeof useTermAgent>
  const wrapper = mount(defineComponent({ setup() { state = useTermAgent({ termId: term, requestedConversationId: requested }); return () => null } }), { global: { plugins: [createClientUi(runtime)] } })
  return { runtime, term, requested, state, wrapper, authorizeNative }
}
it('ignores stale setup responses after the term changes', async () => {
  const resolvers: ((value: AgentSetupResponse) => void)[] = []
  const h = harness(vi.fn(() => new Promise<AgentSetupResponse>((resolve) => resolvers.push(resolve))))
  h.term.value = 't2'; await flushPromises()
  resolvers[1]!(setupResponse('t2')); await flushPromises()
  resolvers[0]!(setupResponse('t1')); await flushPromises()
  expect(h.state.setup.value?.term_id).toBe('t2')
  h.wrapper.unmount()
})
it('rejects a URL conversation owned by another binding', async () => {
  const h = harness(vi.fn(async () => ({ ...setupResponse(), state: 'ready', binding_id: 'b1' })))
  h.runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [{ conversation_id: 'foreign', binding_id: 'b2' }] })) as never
  h.requested.value = 'foreign'; await flushPromises()
  expect(h.state.selectedConversationId.value).toBeNull()
  expect(h.state.error.value).toContain('会话')
  h.wrapper.unmount()
})
it('keeps one idempotency key across a failed user retry', async () => {
  const h = harness(vi.fn(async () => setupResponse()))
  const submit = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(setupResponse())
  h.runtime.api.agents.submitSetup = submit
  await flushPromises()
  const selection = { kind: 'new' as const, profileDisplayName: '助手', paneIds: ['%1'], topologyRevision: 1, disclosureFingerprint: 'fingerprint', accepted: true as const }
  await h.state.submitSetup(selection); await h.state.submitSetup(selection)
  expect(submit.mock.calls[0]![0].idempotency_key).toBe(submit.mock.calls[1]![0].idempotency_key)
  h.wrapper.unmount()
})
it('maps stable server reason codes without exposing raw detail', async () => {
  const h = harness(vi.fn(async () => { throw Object.assign(new ApiError('server', { status: 409, code: 'stale_topology' }), { message: 'raw-secret' }) }))
  await flushPromises()
  expect(h.state.error.value).not.toContain('raw-secret')
  expect(h.state.error.value).toContain('窗格')
  expect(h.authorizeNative).not.toHaveBeenCalled()
  h.wrapper.unmount()
})
it.each(['submitSetup', 'activate'] as const)('retries %s only once after matching sensitive 428', async (action) => {
  const h = harness(vi.fn(async () => ({ ...setupResponse(), binding_id: 'b1' })))
  const operation = vi.fn(async () => { throw new ApiError('server', { status: 428, code: 'sensitive_action_reauthentication_required' }) })
  h.runtime.api.agents.submitSetup = operation
  h.runtime.api.agents.activate = operation
  await flushPromises()
  if (action === 'activate') await h.state.activate()
  else await h.state.submitSetup({ kind: 'new', profileDisplayName: 'Helper', paneIds: ['%1'], topologyRevision: 1, disclosureFingerprint: 'fp', accepted: true })
  expect(h.authorizeNative).toHaveBeenCalledOnce()
  expect(operation).toHaveBeenCalledTimes(2)
  expect(operation.mock.calls[0]).toEqual(operation.mock.calls[1])
  expect(h.state.error.value).toBe('身份验证已失效，请重新操作。')
  h.wrapper.unmount()
})
it('reauthenticates and retries the exact disclosure acceptance once', async () => {
  const disclosure = {
    binding_id: 'b1',
    disclosure_fingerprint: 'current-fingerprint',
    provider_id: 'deepseek',
    model_id: 'deepseek-v4-flash',
    endpoint_origin: 'https://api.deepseek.com',
    region: 'global',
    retention_terms: 'retention terms',
    retention_version: 'retention-v1',
    no_training: true,
    policy_version: 'policy-v1',
    credential_source: 'DEEPSEEK_API_KEY',
    accepted: false,
    accepted_at: null,
    accepted_auth_epoch: null,
  }
  const h = harness(vi.fn(async () => ({
    ...setupResponse(),
    state: 'unavailable',
    binding_id: 'b1',
    disclosure,
    reason_code: 'binding_disclosure_stale',
  })))
  const accept = vi.fn(async () => {
    throw new ApiError('server', { status: 428, code: 'sensitive_action_reauthentication_required' })
  })
  h.runtime.api.agents.acceptDisclosure = accept
  await flushPromises()

  await h.state.acceptDisclosure()

  expect(h.authorizeNative).toHaveBeenCalledOnce()
  expect(accept).toHaveBeenCalledTimes(2)
  expect(accept.mock.calls[0]).toEqual(accept.mock.calls[1])
  expect(accept).toHaveBeenCalledWith(
    'b1',
    { disclosure_fingerprint: 'current-fingerprint', accepted: true },
    expect.any(AbortSignal),
  )
  h.wrapper.unmount()
})
it('selects a newly created conversation even before the host updates its old URL query', async () => {
  const h = harness(vi.fn(async () => ({ ...setupResponse(), state: 'ready', binding_id: 'b1' })))
  const first = { conversation_id: 'c1', binding_id: 'b1' }, second = { conversation_id: 'c2', binding_id: 'b1' }
  h.runtime.api.agents.listConversations = vi.fn(async () => ({ conversations: [first, second] })) as never
  h.runtime.api.agents.createConversation = vi.fn(async () => second) as never
  h.requested.value = 'c1'; await flushPromises()
  expect(h.state.selectedConversationId.value).toBe('c1')
  await h.state.createConversation()
  expect(h.state.selectedConversationId.value).toBe('c2')
  h.wrapper.unmount()
})
