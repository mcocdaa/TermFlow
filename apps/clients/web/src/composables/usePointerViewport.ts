import { reactive } from 'vue'

export interface PointerSample { pointerId: number; x: number; y: number }
export interface ViewportGeometry { width: number; height: number }
export interface PaneGeometry { pane_id: string; left: number; top: number; width: number; height: number }
interface PointerViewportOptions {
  viewport: ViewportGeometry
  content: ViewportGeometry
  canPan?: () => boolean
  emitControl?: (...args: unknown[]) => void
}

const clamp = (value: number, minimum: number, maximum: number) => Math.min(maximum, Math.max(minimum, value))
const distance = (a: PointerSample, b: PointerSample) => Math.hypot(a.x - b.x, a.y - b.y)
const midpoint = (a: PointerSample, b: PointerSample) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 })

export function createPointerViewport(options: PointerViewportOptions) {
  let viewport = options.viewport
  let content = options.content
  const pointers = new Map<number, PointerSample>()
  const state = reactive({ scale: 1, panX: 0, panY: 0, focusedPaneId: null as string | null })
  let pinch: { distance: number; midpoint: { x: number; y: number }; scale: number; panX: number; panY: number } | null = null

  function clampPan() {
    const width = content.width * state.scale
    const height = content.height * state.scale
    state.panX = width <= viewport.width ? (viewport.width - width) / 2 : clamp(state.panX, viewport.width - width, 0)
    state.panY = height <= viewport.height ? (viewport.height - height) / 2 : clamp(state.panY, viewport.height - height, 0)
  }
  function beginPinch() {
    const [a, b] = [...pointers.values()]
    if (!a || !b) return
    pinch = { distance: Math.max(1, distance(a, b)), midpoint: midpoint(a, b), scale: state.scale, panX: state.panX, panY: state.panY }
  }
  function pointerDown(point: PointerSample) { pointers.set(point.pointerId, point); if (pointers.size === 2) beginPinch() }
  function pointerMove(point: PointerSample) {
    const previous = pointers.get(point.pointerId)
    if (!previous) return
    pointers.set(point.pointerId, point)
    if (pointers.size >= 2 && pinch) {
      const [a, b] = [...pointers.values()]
      const currentMidpoint = midpoint(a, b)
      const nextScale = clamp(pinch.scale * distance(a, b) / pinch.distance, 0.25, 4)
      const contentX = (pinch.midpoint.x - pinch.panX) / pinch.scale
      const contentY = (pinch.midpoint.y - pinch.panY) / pinch.scale
      state.scale = nextScale
      state.panX = currentMidpoint.x - contentX * nextScale
      state.panY = currentMidpoint.y - contentY * nextScale
      state.focusedPaneId = null
      clampPan()
      return
    }
    if (pointers.size === 1 && (options.canPan?.() ?? true)) {
      state.panX += point.x - previous.x
      state.panY += point.y - previous.y
      state.focusedPaneId = null
      clampPan()
    }
  }
  function pointerUp(pointerId: number) { pointers.delete(pointerId); pinch = null; if (pointers.size === 2) beginPinch() }
  function setTransform(transform: { scale: number; panX: number; panY: number }) {
    state.scale = clamp(transform.scale, 0.25, 4); state.panX = transform.panX; state.panY = transform.panY; state.focusedPaneId = null; clampPan()
  }
  function updateGeometry(nextViewport: ViewportGeometry, nextContent: ViewportGeometry) { viewport = nextViewport; content = nextContent; clampPan() }
  function focusPane(pane: PaneGeometry, cell: { cellWidth: number; cellHeight: number }) {
    const paneWidth = Math.max(1, pane.width * cell.cellWidth)
    const paneHeight = Math.max(1, pane.height * cell.cellHeight)
    state.scale = clamp(Math.min(viewport.width / paneWidth, viewport.height / paneHeight), 0.25, 4)
    state.panX = (viewport.width - paneWidth * state.scale) / 2 - pane.left * cell.cellWidth * state.scale
    state.panY = (viewport.height - paneHeight * state.scale) / 2 - pane.top * cell.cellHeight * state.scale
    state.focusedPaneId = pane.pane_id
    clampPan()
  }
  function reset() { state.scale = 1; state.panX = 0; state.panY = 0; state.focusedPaneId = null; clampPan() }
  return { state, pointerDown, pointerMove, pointerUp, setTransform, updateGeometry, focusPane, reset }
}
