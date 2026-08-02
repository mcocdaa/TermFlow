import { flushPromises, mount } from '@vue/test-utils'
import QRCode from 'qrcode'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import ServerConnectionPanel from './ServerConnectionPanel.vue'

vi.mock('qrcode', () => ({
  default: { toString: vi.fn().mockResolvedValue('<svg><path /></svg>') },
}))

describe('ServerConnectionPanel', () => {
  it('shows, copies, and opens a credential-free relay URL QR', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const runtime = createFakeRuntime({ clipboard: { writeText } as ClientRuntime['clipboard'] })
    const wrapper = mount(ServerConnectionPanel, {
      attachTo: document.body,
      props: { issuer: 'https://relay.example.com' },
      global: { plugins: [createClientUi(runtime)] },
    })

    expect(wrapper.get('.eyebrow').text()).toBe('Server')
    expect(wrapper.get('#server-heading').text()).toBe('中继服务器')
    expect(wrapper.get('[data-server-label]').text()).toContain('服务网址')
    expect(wrapper.text()).not.toContain('B 连接地址')
    expect(wrapper.get('[data-server-issuer]').text()).toBe('https://relay.example.com')
    await wrapper.get('[data-action="copy-server-url"]').trigger('click')
    expect(writeText).toHaveBeenCalledWith('https://relay.example.com')

    const trigger = wrapper.get('[data-action="show-server-qr"]')
    expect(trigger.attributes('aria-label')).toBe('显示服务网址二维码')
    await trigger.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"]')).toBeTruthy()
    const payload = String(vi.mocked(QRCode.toString).mock.calls.at(-1)?.[0])
    expect(JSON.parse(payload)).toEqual({ protocol: 'termflow-connect-v1', issuer: 'https://relay.example.com' })
    expect(payload).not.toMatch(/token|secret|access_token|refresh_token/i)
    wrapper.unmount()
  })
})
