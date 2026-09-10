import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { clampFloatingGeometry, useFloatingPanel, type FloatingPanelGeometry } from './useFloatingPanel'

function pointerEvent(type: string, values: Partial<Pick<PointerEvent, 'button' | 'clientX' | 'clientY'>> = {}) {
  const event = new Event(type, { bubbles: true, cancelable: true }) as PointerEvent
  for (const [key, value] of Object.entries({ button: 0, clientX: 0, clientY: 0, ...values })) {
    Object.defineProperty(event, key, { configurable: true, value })
  }
  return event
}

describe('clampFloatingGeometry', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps a resized panel within the workspace and above its minimum size', () => {
    const geometry: FloatingPanelGeometry = { left: -40, top: -20, width: 900, height: 700 }
    expect(clampFloatingGeometry(geometry, { width: 640, height: 480 })).toEqual({
      left: 16,
      top: 16,
      width: 608,
      height: 448,
    })
  })

  it('does not let a narrow workspace produce negative available dimensions', () => {
    expect(clampFloatingGeometry(
      { left: 100, top: 100, width: 300, height: 200 },
      { width: 24, height: 20 },
    )).toEqual({ left: 0, top: 0, width: 24, height: 20 })
  })

  it('defaults to the top-right corner when the panel has not been laid out yet', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('host')
        ? ({ left: 0, top: 0, right: 640, bottom: 480, width: 640, height: 480 } as DOMRect)
        : ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 } as DOMRect)
    })
    const Host = defineComponent({
      setup() {
        const container = ref<HTMLElement | null>(null)
        const panel = ref<HTMLElement | null>(null)
        const floating = useFloatingPanel({ container, panel, defaultWidth: 480, defaultHeight: 360 })
        return { container, panel, style: floating.style }
      },
      template: '<div ref="container" class="host"><aside ref="panel" :style="style" /></div>',
    })
    const wrapper = mount(Host)
    await flushPromises()
    expect(wrapper.get('aside').attributes('style')).toContain('left: 144px')
    expect(wrapper.get('aside').attributes('style')).toContain('top: 16px')
    wrapper.unmount()
  })

  it('moves and resizes the panel from pointer interactions', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('host')
        ? ({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 } as DOMRect)
        : ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 } as DOMRect)
    })
    const Host = defineComponent({
      setup() {
        const container = ref<HTMLElement | null>(null)
        const panel = ref<HTMLElement | null>(null)
        const floating = useFloatingPanel({ container, panel, defaultWidth: 320, defaultHeight: 240 })
        return { container, panel, ...floating }
      },
      template: '<div ref="container" class="host"><aside ref="panel" :style="style" /></div>',
    })
    const wrapper = mount(Host)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      geometry: FloatingPanelGeometry
      beginDrag: (event: PointerEvent) => void
      beginResize: (edge: 'se', event: PointerEvent) => void
      stopInteraction: () => void
    }
    const beforeDrag = { ...vm.geometry }
    const down = pointerEvent('pointerdown', { clientX: 100, clientY: 120 })
    expect(down.clientX).toBe(100)
    vm.beginDrag(down)
    // The public controller exposes the active interaction, making this test
    // independent of browser-specific pointer-event constructors.
    expect((wrapper.vm as unknown as { interaction: string | null }).interaction).toBe('drag')
    const move = pointerEvent('pointermove', { clientX: 55, clientY: 155 })
    expect(move.clientX).toBe(55)
    window.dispatchEvent(move)
    expect(vm.geometry.left).toBe(beforeDrag.left - 45)
    await flushPromises()
    expect(vm.geometry.left).toBe(beforeDrag.left - 45)
    expect(vm.geometry.top).toBe(beforeDrag.top + 35)
    vm.stopInteraction()

    const beforeResize = { ...vm.geometry }
    vm.beginResize('se', pointerEvent('pointerdown', { clientX: 200, clientY: 220 }))
    window.dispatchEvent(pointerEvent('pointermove', { clientX: 260, clientY: 270 }))
    await flushPromises()
    expect(vm.geometry.width).toBe(beforeResize.width + 60)
    expect(vm.geometry.height).toBe(beforeResize.height + 50)
    vm.stopInteraction()
    wrapper.unmount()
  })
})
