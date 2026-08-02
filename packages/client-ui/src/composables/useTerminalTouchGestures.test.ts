import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTerminalTouchGestures } from './useTerminalTouchGestures'

const point = (pointerId: number, x: number, y: number) => ({ pointerId, x, y })

function harness(locked = false) {
  let currentLocked = locked
  let currentConnected = true
  const viewport = { pointerDown: vi.fn(), pointerMove: vi.fn(), pointerUp: vi.fn() }
  const dispatchMouse = vi.fn()
  const gestures = createTerminalTouchGestures({
    locked: () => currentLocked,
    connected: () => currentConnected,
    viewport,
    dispatchMouse,
    longPressMs: 500,
    moveSlop: 8,
  })
  return {
    gestures,
    viewport,
    dispatchMouse,
    setLocked: (value: boolean) => { currentLocked = value },
    setConnected: (value: boolean) => { currentConnected = value },
  }
}

afterEach(() => vi.useRealTimers())

describe('terminal touch gestures', () => {
  it('delegates unlocked one- and two-pointer gestures without terminal mouse events', () => {
    const { gestures, viewport, dispatchMouse } = harness(false)
    gestures.pointerDown(point(1, 100, 200))
    gestures.pointerDown(point(2, 200, 200))
    gestures.pointerMove(point(2, 240, 200))
    gestures.pointerUp(1)
    gestures.pointerUp(2)
    expect(viewport.pointerDown).toHaveBeenCalledTimes(2)
    expect(viewport.pointerMove).toHaveBeenCalledWith(point(2, 240, 200))
    expect(dispatchMouse).not.toHaveBeenCalled()
  })

  it('turns a locked tap and drag into complete left-button lifecycles', () => {
    const { gestures, dispatchMouse } = harness(true)
    gestures.pointerDown(point(1, 50, 60))
    gestures.pointerUp(1, point(1, 50, 60))
    expect(dispatchMouse.mock.calls.map(([event]) => event.type)).toEqual(['mousedown', 'mouseup'])
    dispatchMouse.mockClear()
    gestures.pointerDown(point(2, 70, 80))
    gestures.pointerMove(point(2, 90, 80))
    gestures.pointerUp(2, point(2, 100, 80))
    expect(dispatchMouse.mock.calls.map(([event]) => event.type)).toEqual(['mousedown', 'mousemove', 'mouseup'])
  })

  it('turns a locked long press into word selection without a remote mouse down', () => {
    vi.useFakeTimers()
    const { gestures, dispatchMouse } = harness(true)
    gestures.pointerDown(point(1, 50, 60))
    vi.advanceTimersByTime(500)
    expect(dispatchMouse).toHaveBeenCalledWith(expect.objectContaining({ type: 'mousedown', detail: 2, forceSelection: true }))
    gestures.pointerMove(point(1, 100, 60))
    gestures.pointerUp(1, point(1, 100, 60))
    expect(dispatchMouse.mock.calls.every(([event]) => event.forceSelection === true)).toBe(true)
  })

  it('does not pan a locked viewport while disconnected', () => {
    const { gestures, viewport, dispatchMouse, setConnected } = harness(true)
    setConnected(false)
    gestures.pointerDown(point(1, 100, 100))
    gestures.pointerMove(point(1, 40, 40))
    gestures.pointerUp(1, point(1, 40, 40))
    expect(viewport.pointerDown).not.toHaveBeenCalled()
    expect(viewport.pointerMove).not.toHaveBeenCalled()
    expect(dispatchMouse).not.toHaveBeenCalled()
  })

  it('cancels timers and releases an active button on a second touch or mode change', () => {
    vi.useFakeTimers()
    const { gestures, dispatchMouse, setLocked } = harness(true)
    gestures.pointerDown(point(1, 10, 10))
    gestures.pointerMove(point(1, 30, 10))
    gestures.pointerDown(point(2, 40, 10))
    expect(dispatchMouse.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ type: 'mouseup', buttons: 0 }))
    setLocked(false)
    gestures.cancelAll()
    vi.runAllTimers()
    expect(vi.getTimerCount()).toBe(0)
  })
})
