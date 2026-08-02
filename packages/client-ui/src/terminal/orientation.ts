import type { PointerViewportSnapshot } from '../composables/usePointerViewport'
import type { DisplayMode } from './viewport'

export type TerminalOrientation = 'portrait' | 'landscape'
export interface OrientationView {
  displayMode: DisplayMode
  viewport: PointerViewportSnapshot | null
}

export function orientationFor(width: number, height: number): TerminalOrientation {
  return height > width ? 'portrait' : 'landscape'
}

export function createOrientationViewState(): Record<TerminalOrientation, OrientationView> {
  return {
    portrait: { displayMode: 'font-100', viewport: null },
    landscape: { displayMode: 'fit', viewport: null },
  }
}
