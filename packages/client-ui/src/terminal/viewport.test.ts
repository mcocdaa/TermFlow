import { describe, expect, it } from 'vitest'
import { displayPresentation, type DisplayMode } from './viewport'

describe('client-only terminal display presentation', () => {
  const grid = { rows: 40, cols: 120 }
  const metrics = { cellWidth: 10, cellHeight: 20 }

  it.each([
    ['scale-50', 0.5],
    ['scale-75', 0.75],
    ['font-100', 1],
  ] as Array<[DisplayMode, number]>)('maps %s to an xterm visual scale', (mode, scale) => {
    expect(displayPresentation(mode, grid, { width: 1000, height: 600 }, metrics)).toEqual(expect.objectContaining({ scale, gridWidth: 1200, gridHeight: 800 }))
  })

  it('fits the complete authoritative grid uniformly inside the viewport', () => {
    const presentation = displayPresentation('fit', grid, { width: 900, height: 500 }, metrics)
    expect(presentation.scale).toBe(0.625)
    expect(presentation.scaledWidth).toBe(750)
    expect(presentation.scaledHeight).toBe(500)
  })

  it('never produces rows, cols, or a resize control payload', () => {
    const presentation = displayPresentation('scale-75', grid, { width: 360, height: 800 }, metrics)
    expect(Object.keys(presentation)).not.toContain('rows')
    expect(Object.keys(presentation)).not.toContain('cols')
    expect(JSON.stringify(presentation)).not.toContain('terminal.resize')
  })
})
