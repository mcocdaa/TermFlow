import { flushPromises, mount } from '@vue/test-utils'
import QRCode from 'qrcode'
import { describe, expect, it, vi } from 'vitest'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { createThemeState } from '../../theme/theme'
import { createClientUi } from '../../runtime'
import ThemedQrCode from './ThemedQrCode.vue'

vi.mock('qrcode', () => ({
  default: { toString: vi.fn().mockResolvedValue('<svg><path /></svg>') },
}))

describe('ThemedQrCode', () => {
  it('renders SVG with semantic colors and reacts to theme changes', async () => {
    document.documentElement.style.setProperty('--color-qr-foreground', 'oklch(20% 0.1 250)')
    document.documentElement.style.setProperty('--color-qr-background', 'oklch(95% 0.02 250)')
    const theme = createThemeState(
      { load: () => null, save: () => undefined },
      { apply: (value) => document.documentElement.setAttribute('data-theme', value) },
    )
    const wrapper = mount(ThemedQrCode, {
      props: { value: 'termflow://relay', alt: '中继服务器二维码' },
      global: { plugins: [createClientUi(createFakeRuntime(), { theme })] },
    })

    await flushPromises()
    expect(QRCode.toString).toHaveBeenLastCalledWith('termflow://relay', {
      type: 'svg',
      errorCorrectionLevel: 'M',
      margin: 4,
      color: { dark: 'oklch(20% 0.1 250)', light: 'oklch(95% 0.02 250)' },
    })
    expect(wrapper.get('img').attributes('src')).toMatch(/^data:image\/svg\+xml/)
    expect(wrapper.get('img').attributes('alt')).toBe('中继服务器二维码')

    theme.select('cloud-cobalt')
    await flushPromises()
    expect(QRCode.toString).toHaveBeenCalledTimes(2)
  })
})
