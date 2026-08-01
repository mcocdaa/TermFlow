export type DisplayMode = 'scale-50' | 'scale-75' | 'font-100' | 'fit'
export interface GridSize { rows: number; cols: number }
export interface ViewportSize { width: number; height: number }
export interface CellMetrics { cellWidth: number; cellHeight: number }
export interface DisplayPresentation { scale: number; gridWidth: number; gridHeight: number; scaledWidth: number; scaledHeight: number }

export function displayPresentation(mode: DisplayMode, grid: GridSize, viewport: ViewportSize, metrics: CellMetrics): DisplayPresentation {
  const gridWidth = grid.cols * metrics.cellWidth
  const gridHeight = grid.rows * metrics.cellHeight
  const fixed = mode === 'scale-50' ? 0.5 : mode === 'scale-75' ? 0.75 : 1
  const fit = Math.min(viewport.width / gridWidth, viewport.height / gridHeight)
  const scale = mode === 'fit' ? Math.max(0.1, Math.min(2, Number.isFinite(fit) ? fit : 1)) : fixed
  return { scale, gridWidth, gridHeight, scaledWidth: gridWidth * scale, scaledHeight: gridHeight * scale }
}
