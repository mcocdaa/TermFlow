import { computed, nextTick, onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

export interface FloatingPanelGeometry {
  left: number
  top: number
  width: number
  height: number
}

export type FloatingPanelResizeEdge = 'n' | 'e' | 's' | 'w' | 'ne' | 'se' | 'sw' | 'nw'

export interface FloatingPanelBounds {
  width: number
  height: number
}

export interface FloatingPanelOptions {
  container: Ref<HTMLElement | null>
  panel: Ref<HTMLElement | null>
  margin?: number
  minWidth?: number
  minHeight?: number
  defaultWidth?: number
  defaultHeight?: number
}

const finite = (value: number, fallback: number) => Number.isFinite(value) ? value : fallback

/**
 * Keep a floating panel inside its workspace. The margin is deliberately
 * part of the clamp contract: the card never touches the canvas edge while
 * there is room, and collapses that margin gracefully on very small hosts.
 */
export function clampFloatingGeometry(
  input: FloatingPanelGeometry,
  bounds: FloatingPanelBounds,
  options: Pick<FloatingPanelOptions, 'margin' | 'minWidth' | 'minHeight'> = {},
): FloatingPanelGeometry {
  const margin = Math.max(0, options.margin ?? 16)
  const hostWidth = Math.max(0, finite(bounds.width, 0))
  const hostHeight = Math.max(0, finite(bounds.height, 0))
  const availableWidth = hostWidth > margin * 2 ? hostWidth - margin * 2 : hostWidth
  const availableHeight = hostHeight > margin * 2 ? hostHeight - margin * 2 : hostHeight
  const minWidth = Math.max(0, options.minWidth ?? 280)
  const minHeight = Math.max(0, options.minHeight ?? 220)
  const width = Math.min(Math.max(minWidth, finite(input.width, minWidth)), availableWidth)
  const height = Math.min(Math.max(minHeight, finite(input.height, minHeight)), availableHeight)
  const edgeMarginX = hostWidth > margin * 2 ? margin : 0
  const edgeMarginY = hostHeight > margin * 2 ? margin : 0
  const left = Math.min(
    Math.max(edgeMarginX, finite(input.left, edgeMarginX)),
    Math.max(edgeMarginX, hostWidth - width - edgeMarginX),
  )
  const top = Math.min(
    Math.max(edgeMarginY, finite(input.top, edgeMarginY)),
    Math.max(edgeMarginY, hostHeight - height - edgeMarginY),
  )
  return { left, top, width, height }
}

/** Pointer-driven drag/resize controller for the terminal Agent card. */
export function useFloatingPanel(options: FloatingPanelOptions) {
  const geometry = ref<FloatingPanelGeometry>({
    left: Number.MAX_SAFE_INTEGER,
    top: options.margin ?? 16,
    width: options.defaultWidth ?? 480,
    height: options.defaultHeight ?? 620,
  })
  const active = ref(false)
  const interaction = ref<'drag' | FloatingPanelResizeEdge | null>(null)
  let startPointer = { x: 0, y: 0 }
  let startGeometry = { ...geometry.value }

  const style = computed<Record<string, string>>(() => active.value ? {
    left: `${geometry.value.left}px`,
    top: `${geometry.value.top}px`,
    width: `${geometry.value.width}px`,
    height: `${geometry.value.height}px`,
    right: 'auto',
    bottom: 'auto',
  } : {})

  function hostBounds(): { rect: DOMRect; bounds: FloatingPanelBounds } | null {
    const container = options.container.value ?? options.panel.value?.parentElement ?? null
    if (container === null) return null
    const rect = container.getBoundingClientRect()
    const width = rect.width || container.clientWidth
    const height = rect.height || container.clientHeight
    if (width <= 0 || height <= 0) return null
    return { rect, bounds: { width, height } }
  }

  function sync() {
    const host = hostBounds()
    const panel = options.panel.value
    if (host === null) return
    const panelRect = panel?.getBoundingClientRect()
    const width = panelRect?.width || panel?.offsetWidth || options.defaultWidth || geometry.value.width
    const height = panelRect?.height || panel?.offsetHeight || options.defaultHeight || geometry.value.height
    const panelHasLayout = panelRect !== undefined && panelRect.width > 0 && panelRect.height > 0
    const left = panelHasLayout ? panelRect.left - host.rect.left : geometry.value.left
    const top = panelHasLayout ? panelRect.top - host.rect.top : geometry.value.top
    geometry.value = clampFloatingGeometry(
      { left, top, width, height },
      host.bounds,
      options,
    )
    active.value = true
  }

  function updateFromPointer(event: PointerEvent) {
    const host = hostBounds()
    if (host === null || interaction.value === null) return
    const dx = event.clientX - startPointer.x
    const dy = event.clientY - startPointer.y
    const next = { ...startGeometry }
    if (interaction.value === 'drag') {
      next.left += dx
      next.top += dy
    } else {
      const edge = interaction.value
      if (edge.includes('e')) next.width += dx
      if (edge.includes('s')) next.height += dy
      if (edge.includes('w')) { next.left += dx; next.width -= dx }
      if (edge.includes('n')) { next.top += dy; next.height -= dy }
    }
    geometry.value = clampFloatingGeometry(next, host.bounds, options)
  }

  function stopInteraction() {
    interaction.value = null
    window.removeEventListener('pointermove', updateFromPointer)
    window.removeEventListener('pointerup', stopInteraction)
    window.removeEventListener('pointercancel', stopInteraction)
  }

  function startInteraction(kind: 'drag' | FloatingPanelResizeEdge, event: PointerEvent) {
    if (event.button !== undefined && event.button !== 0) return
    event.preventDefault()
    sync()
    interaction.value = kind
    startPointer = { x: event.clientX, y: event.clientY }
    startGeometry = { ...geometry.value }
    window.addEventListener('pointermove', updateFromPointer)
    window.addEventListener('pointerup', stopInteraction)
    window.addEventListener('pointercancel', stopInteraction)
  }

  function beginDrag(event: PointerEvent) { startInteraction('drag', event) }
  function beginResize(edge: FloatingPanelResizeEdge, event: PointerEvent) { startInteraction(edge, event) }

  let observer: ResizeObserver | null = null
  onMounted(() => {
    void nextTick(() => {
      sync()
      const container = options.container.value ?? options.panel.value?.parentElement ?? null
      if (container !== null) {
        observer = new ResizeObserver(sync)
        observer.observe(container)
      }
    })
  })
  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = null
    stopInteraction()
  })

  return { geometry, style, active, interaction, sync, beginDrag, beginResize, stopInteraction }
}
