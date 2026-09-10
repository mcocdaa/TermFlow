import { describe, expect, it, vi } from 'vitest'
import { createPointerViewport } from './usePointerViewport'

describe('mobile pointer viewport', () => {
  it('pinch-zooms around the gesture midpoint without emitting terminal controls', () => {
    const emitControl = vi.fn()
    const viewport = createPointerViewport({ viewport: { width: 360, height: 800 }, content: { width: 1200, height: 600 }, emitControl })
    viewport.pointerDown({ pointerId: 1, x: 100, y: 300 })
    viewport.pointerDown({ pointerId: 2, x: 200, y: 300 })
    viewport.pointerMove({ pointerId: 2, x: 250, y: 300 })
    expect(viewport.state.scale).toBeGreaterThan(1)
    expect(emitControl).not.toHaveBeenCalled()
  })

  it('pans with one pointer only when selection and mouse reporting are inactive', () => {
    let canPan = false
    const viewport = createPointerViewport({ viewport: { width: 360, height: 800 }, content: { width: 1200, height: 600 }, canPan: () => canPan })
    viewport.pointerDown({ pointerId: 1, x: 200, y: 300 })
    viewport.pointerMove({ pointerId: 1, x: 100, y: 300 })
    expect(viewport.state.panX).toBe(0)
    viewport.pointerUp(1)
    canPan = true
    viewport.pointerDown({ pointerId: 2, x: 200, y: 300 })
    viewport.pointerMove({ pointerId: 2, x: 100, y: 300 })
    expect(viewport.state.panX).toBe(-100)
  })

  it('preserves scale and clamps pan across orientation changes', () => {
    const viewport = createPointerViewport({ viewport: { width: 360, height: 800 }, content: { width: 1200, height: 720 } })
    viewport.setTransform({ scale: 2, panX: -1000, panY: -500 })
    viewport.updateGeometry({ width: 800, height: 360 }, { width: 1200, height: 720 })
    expect(viewport.state.scale).toBe(2)
    expect(viewport.state.panX).toBeGreaterThanOrEqual(800 - 2400)
    expect(viewport.state.panY).toBeGreaterThanOrEqual(360 - 1440)
  })

  it('captures and restores only generic scale and pan', () => {
    const viewport = createPointerViewport({ viewport: { width: 360, height: 800 }, content: { width: 1200, height: 720 } })
    viewport.setTransform({ scale: 2, panX: -30, panY: -40 })
    const snapshot = viewport.snapshot()
    expect(snapshot).toEqual({ scale: 2, panX: -30, panY: -40 })
    expect(viewport).not.toHaveProperty('focusPane')
    viewport.reset()
    viewport.restore(snapshot)
    expect(viewport.snapshot()).toEqual(snapshot)
  })
})
