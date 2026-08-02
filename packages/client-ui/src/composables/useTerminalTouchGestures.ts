import type { PointerSample } from './usePointerViewport'
import type { TerminalMouseDispatch } from '../terminal/xtermAdapter'

interface ViewportPointerSink {
  pointerDown(point: PointerSample): void
  pointerMove(point: PointerSample): void
  pointerUp(pointerId: number): void
}

interface TerminalTouchGestureOptions {
  locked(): boolean
  connected(): boolean
  viewport: ViewportPointerSink
  dispatchMouse(event: TerminalMouseDispatch): void
  longPressMs?: number
  moveSlop?: number
}

type LockedPhase = 'pending' | 'remote' | 'selection'

interface LockedGesture {
  pointerId: number
  start: PointerSample
  current: PointerSample
  phase: LockedPhase
  timer: ReturnType<typeof setTimeout> | null
}

const mouse = (type: TerminalMouseDispatch['type'], point: PointerSample, forceSelection = false, detail: 1 | 2 = 1): TerminalMouseDispatch => ({
  type,
  clientX: point.x,
  clientY: point.y,
  buttons: type === 'mouseup' ? 0 : 1,
  button: 0,
  detail,
  forceSelection,
})

export function createTerminalTouchGestures(options: TerminalTouchGestureOptions) {
  const longPressMs = options.longPressMs ?? 500
  const moveSlop = options.moveSlop ?? 8
  const viewportPointers = new Set<number>()
  const lockedPointers = new Set<number>()
  let active: LockedGesture | null = null
  let lockedBlocked = false

  function clearTimer() {
    if (active?.timer !== null && active?.timer !== undefined) clearTimeout(active.timer)
    if (active) active.timer = null
  }

  function finishLocked(end = active?.current) {
    if (!active || !end) return
    clearTimer()
    if (active.phase === 'remote' || active.phase === 'selection') {
      const selecting = active.phase === 'selection'
      options.dispatchMouse(mouse('mouseup', end, selecting, selecting ? 2 : 1))
    }
    active = null
  }

  function pointerDown(point: PointerSample) {
    if (!options.locked()) {
      viewportPointers.add(point.pointerId)
      options.viewport.pointerDown(point)
      return
    }
    lockedPointers.add(point.pointerId)
    if (!options.connected()) return
    if (lockedBlocked) return
    if (active) {
      finishLocked()
      lockedBlocked = true
      return
    }
    active = { pointerId: point.pointerId, start: point, current: point, phase: 'pending', timer: null }
    active.timer = setTimeout(() => {
      if (!active || active.phase !== 'pending') return
      active.phase = 'selection'
      options.dispatchMouse(mouse('mousedown', active.start, true, 2))
    }, longPressMs)
  }

  function pointerMove(point: PointerSample) {
    if (viewportPointers.has(point.pointerId)) {
      options.viewport.pointerMove(point)
      return
    }
    if (lockedBlocked || !active || active.pointerId !== point.pointerId) return
    active.current = point
    if (active.phase === 'pending') {
      if (Math.hypot(point.x - active.start.x, point.y - active.start.y) < moveSlop) return
      clearTimer()
      active.phase = 'remote'
      options.dispatchMouse(mouse('mousedown', active.start))
      options.dispatchMouse(mouse('mousemove', point))
      return
    }
    const selecting = active.phase === 'selection'
    options.dispatchMouse(mouse('mousemove', point, selecting, selecting ? 2 : 1))
  }

  function pointerUp(pointerId: number, point?: PointerSample) {
    if (viewportPointers.delete(pointerId)) {
      options.viewport.pointerUp(pointerId)
      return
    }
    lockedPointers.delete(pointerId)
    if (lockedBlocked) {
      if (lockedPointers.size === 0) lockedBlocked = false
      return
    }
    if (!active || active.pointerId !== pointerId) return
    const end = point ?? active.current
    if (active.phase === 'pending') {
      clearTimer()
      options.dispatchMouse(mouse('mousedown', end))
      options.dispatchMouse(mouse('mouseup', end))
      active = null
      return
    }
    finishLocked(end)
  }

  function pointerCancel(pointerId: number, point?: PointerSample) {
    if (viewportPointers.delete(pointerId)) {
      options.viewport.pointerUp(pointerId)
      return
    }
    lockedPointers.delete(pointerId)
    if (active?.pointerId === pointerId) finishLocked(point ?? active.current)
    if (lockedPointers.size === 0) lockedBlocked = false
  }

  function cancelAll() {
    for (const pointerId of viewportPointers) options.viewport.pointerUp(pointerId)
    viewportPointers.clear()
    finishLocked()
    lockedPointers.clear()
    lockedBlocked = false
  }

  return { pointerDown, pointerMove, pointerUp, pointerCancel, cancelAll, dispose: cancelAll }
}
