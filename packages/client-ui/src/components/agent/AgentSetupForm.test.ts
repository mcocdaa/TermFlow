import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentSetupForm from './AgentSetupForm.vue'
const disclosure = { disclosure_fingerprint: 'fp', provider_id: 'provider', model_id: 'model', endpoint_origin: 'https://provider.example', region: 'region', retention_terms: 'retention', retention_version: 'retention-v2', no_training: true, policy_version: 'policy-v3', credential_source: 'DEEPSEEK_API_KEY' }
const setup = { topology_revision: 7, disclosure } as never
const mountForm = (profiles: unknown[] = []) => mount(AgentSetupForm, { props: { setup, profiles: profiles as never, panes: [{ pane_id: '%1', title: 'shell' }] as never, busy: false, error: '' } })
it('selects every current pane by default and requires at least one pane', async () => {
  const w = mountForm()
  expect((w.get('[name="paneIds"]').element as HTMLInputElement).checked).toBe(true)
  await w.get('[data-action="clear-panes"]').trigger('click')
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')).toBeUndefined()
  expect(w.text()).toContain('请至少选择一个当前窗格')
  await w.get('[data-action="select-all-panes"]').trigger('click')
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')).toHaveLength(1)
  w.unmount()
})
it('emits numeric topology revision, current fingerprint and automatic acceptance', async () => {
  const w = mountForm()
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')?.[0]?.[0]).toEqual({ kind: 'new', profileDisplayName: 'provider · model', paneIds: ['%1'], topologyRevision: 7, disclosureFingerprint: 'fp', accepted: true })
  w.unmount()
})
it('reuses an existing profile when its provider and model match the deployment', async () => {
  const w = mountForm([
    { profile_id: 'p1', display_name: 'Matching', backend_kind: 'opencode', provider_id: 'provider', model_id: 'model' },
    { profile_id: 'p2', display_name: 'Other provider', backend_kind: 'opencode', provider_id: 'other', model_id: 'model' },
  ])
  await w.get('form').trigger('submit')
  expect(w.emitted('submit')?.[0]?.[0]).toEqual({ kind: 'existing', profileId: 'p1', paneIds: ['%1'], topologyRevision: 7, disclosureFingerprint: 'fp', accepted: true })
  w.unmount()
})
it('retains pane selection after a server error', async () => {
  const w = mountForm()
  await w.get('[name="paneIds"]').setValue(false)
  await w.setProps({ error: '服务器暂不可用' })
  expect((w.get('[name="paneIds"]').element as HTMLInputElement).checked).toBe(false)
  w.unmount()
})
it('renders only pane inputs plus hover help, never credential or JSON inputs', () => {
  const w = mountForm()
  expect(w.findAll('input').map((input) => input.attributes('name'))).toEqual(['paneIds'])
  expect(w.find('textarea').exists()).toBe(false)
  expect(w.text()).not.toContain('https://provider.example')
  expect(w.find('[role="tooltip"]').exists()).toBe(true)
  w.unmount()
})
it('shows the deployment model without offering a change control', () => {
  const w = mountForm()
  expect(w.text()).toContain('provider · model')
  expect(w.find('select').exists()).toBe(false)
  w.unmount()
})
