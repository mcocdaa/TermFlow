import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { expect, it, vi } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { useSensitiveAuthorization } from '../../composables/useSensitiveAuthorization'
import Dialog from './AgentSensitiveReauthDialog.vue'
it.each(['browser-session', 'native-oauth'] as const)('keeps %s credentials within its platform', async (mode) => {
  const authorizeNative = vi.fn(() => new Promise<'authenticated'>(() => {}))
  const runtime = createFakeRuntime({ sensitiveAuthorization: { mode, authorizeNative } })
  runtime.api.sessions.login = vi.fn(async () => ({ authenticated: true, expires_at: '2026-09-08T00:00:00Z' }))
  let auth!: ReturnType<typeof useSensitiveAuthorization>
  const w = mount(defineComponent({ components: { Dialog }, setup() { auth = useSensitiveAuthorization(); return {} }, template: '<Dialog />' }), { attachTo: document.body, global: { plugins: [createClientUi(runtime)] } })
  const controller = new AbortController()
  const pending = auth.authorize(controller.signal); await flushPromises()
  if (mode === 'native-oauth') {
    expect(w.findAll('input')).toHaveLength(0)
    expect(authorizeNative).toHaveBeenCalledOnce()
    await w.get('button').trigger('click')
    expect(await pending).toBe(false)
    expect((authorizeNative.mock.calls[0] as unknown as [AbortSignal])[0].aborted).toBe(true)
  } else {
    await w.get('[name="root_credential"]').setValue('root-secret')
    await w.get('form').trigger('submit'); await flushPromises()
    expect(await pending).toBe(true)
    expect(runtime.api.sessions.login).toHaveBeenCalledWith('root-secret', expect.any(AbortSignal))
    expect(w.find('input').exists()).toBe(false)
    expect(authorizeNative).not.toHaveBeenCalled()
  }
  controller.abort(); w.unmount()
})

it('clears root and TOTP inputs on failed attempts and completes the session challenge', async () => {
  const runtime = createFakeRuntime()
  runtime.api.sessions.login = vi.fn().mockRejectedValueOnce(new Error('SECRET_SERVER_DETAIL')).mockResolvedValueOnce({ status: 'totp_required', challenge_id: 'challenge', expires_at: '2030-01-01' })
  runtime.api.sessions.completeTotp = vi.fn().mockRejectedValueOnce(new Error('SECRET_SERVER_DETAIL')).mockResolvedValueOnce({ authenticated: true, expires_at: '2030-01-01' })
  let auth!: ReturnType<typeof useSensitiveAuthorization>
  const w = mount(defineComponent({ components: { Dialog }, setup() { auth = useSensitiveAuthorization(); return {} }, template: '<Dialog />' }), { attachTo: document.body, global: { plugins: [createClientUi(runtime)] } })
  const pending = auth.authorize(); await flushPromises()
  await w.get('[name="root_credential"]').setValue('root-secret')
  await w.get('form').trigger('submit'); await flushPromises()
  expect((w.get('[name="root_credential"]').element as HTMLInputElement).value).toBe('')
  expect(w.text()).not.toContain('SECRET_SERVER_DETAIL')
  await w.get('[name="root_credential"]').setValue('root-secret')
  await w.get('form').trigger('submit'); await flushPromises()
  expect(w.find('[name="root_credential"]').exists()).toBe(false)
  await w.get('[name="totp"]').setValue('123456')
  await w.get('form').trigger('submit'); await flushPromises()
  expect((w.get('[name="totp"]').element as HTMLInputElement).value).toBe('')
  await w.get('[name="totp"]').setValue('654321')
  await w.get('form').trigger('submit'); await flushPromises()
  expect(runtime.api.sessions.completeTotp).toHaveBeenLastCalledWith('challenge', '654321', expect.any(AbortSignal))
  expect(await pending).toBe(true)
  w.unmount()
})
