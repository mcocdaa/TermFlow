import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentSetupForm from './AgentSetupForm.vue'
const setup = { topology_revision: 7, disclosure: { disclosure_fingerprint: 'fp', provider_id: 'provider', model_id: 'model', endpoint_origin: 'https://provider.example', region: 'region', retention_terms: 'retention', retention_version: 'retention-v2', no_training: true, policy_version: 'policy-v3', credential_source: 'DEEPSEEK_API_KEY' } } as never
const mountForm = () => mount(AgentSetupForm, { props: { setup, profiles: [], panes: [{ pane_id: '%1', title: 'shell' }] as never, busy: false, error: '' } })
it('requires at least one exact pane and explicit disclosure consent', async () => {
  const w = mountForm()
  await w.get('[name="profileDisplayName"]').setValue('Helper')
  await w.get('form').trigger('submit'); expect(w.emitted('submit')).toBeUndefined()
  await w.get('[name="paneIds"]').setValue(true)
  await w.get('form').trigger('submit'); expect(w.emitted('submit')).toBeUndefined()
  await w.get('[name="accepted"]').setValue(true)
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')).toHaveLength(1)
  w.unmount()
})
it('emits numeric topology revision and current fingerprint', async () => {
  const w = mountForm()
  await w.get('[name="profileDisplayName"]').setValue('Helper')
  await w.get('[name="paneIds"]').setValue(true)
  await w.get('[name="accepted"]').setValue(true)
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')?.[0]?.[0]).toEqual({ kind: 'new', profileDisplayName: 'Helper', paneIds: ['%1'], topologyRevision: 7, disclosureFingerprint: 'fp', accepted: true })
  w.unmount()
})
it('retains user selection after a server error', async () => {
  const w = mountForm()
  await w.get('[name="profileDisplayName"]').setValue('Helper')
  await w.get('[name="paneIds"]').setValue(true)
  await w.get('[name="accepted"]').setValue(true)
  await w.setProps({ error: '服务器暂不可用' })
  expect((w.get('[name="paneIds"]').element as HTMLInputElement).checked).toBe(true)
  expect((w.get('[name="profileDisplayName"]').element as HTMLInputElement).value).toBe('Helper')
  expect((w.get('[name="accepted"]').element as HTMLInputElement).checked).toBe(true)
  w.unmount()
})
it('never renders endpoint key token or arbitrary JSON inputs', () => {
  const w = mountForm()
  expect(w.findAll('input').map((input) => input.attributes('name'))).toEqual(['profileDisplayName', 'paneIds', 'accepted'])
  expect(w.find('textarea').exists()).toBe(false)
  w.unmount()
})
it('shows every canonical provider disclosure field before consent', () => {
  const w = mountForm()
  expect(w.text()).toContain('https://provider.example')
  expect(w.text()).toContain('region')
  expect(w.text()).toContain('retention')
  expect(w.text()).toContain('retention-v2')
  expect(w.text()).toContain('policy-v3')
  expect(w.text()).toContain('DEEPSEEK_API_KEY')
  expect(w.text()).toContain('fp')
  w.unmount()
})
it('allows an existing profile only when its provider and model match the current disclosure', async () => {
  const profiles = [
    { profile_id: 'p1', display_name: 'Matching', backend_kind: 'opencode', provider_id: 'provider', model_id: 'model' },
    { profile_id: 'p2', display_name: 'Other provider', backend_kind: 'opencode', provider_id: 'other', model_id: 'model' },
  ]
  const w = mount(AgentSetupForm, { props: { setup, profiles, panes: [{ pane_id: '%1', title: 'shell' }] as never, busy: false, error: '' } })
  expect(w.get('option[value="p2"]').attributes('disabled')).toBeDefined()
  await w.get('[name="paneIds"]').setValue(true)
  await w.get('[name="accepted"]').setValue(true)
  await w.get('[name="profileId"]').setValue('p2')
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')).toBeUndefined()
  await w.get('[name="profileId"]').setValue('p1')
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')?.[0]?.[0]).toEqual({ kind: 'existing', profileId: 'p1', paneIds: ['%1'], topologyRevision: 7, disclosureFingerprint: 'fp', accepted: true })
  w.unmount()
})
