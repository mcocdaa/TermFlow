import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const requiredTokens = [
  'color-page', 'color-panel', 'color-elevated', 'color-terminal', 'color-terminal-foreground',
  'color-terminal-selection', 'terminal-black', 'terminal-red', 'terminal-green',
  'terminal-yellow', 'terminal-blue', 'terminal-magenta', 'terminal-cyan', 'terminal-white',
  'terminal-bright-black', 'terminal-bright-red', 'terminal-bright-green',
  'terminal-bright-yellow', 'terminal-bright-blue', 'terminal-bright-magenta',
  'terminal-bright-cyan', 'terminal-bright-white', 'color-border',
  'color-text-primary', 'color-text-secondary', 'color-text-muted', 'color-accent',
  'color-accent-contrast', 'color-focus', 'color-online', 'color-warning', 'color-danger',
  'color-qr-foreground', 'color-qr-background',
  'shadow-panel', 'radius-sm', 'radius-md', 'radius-lg', 'space-1', 'space-2', 'space-3',
  'space-4', 'space-5', 'font-ui', 'font-mono',
]

describe('design token contract', () => {
  const workspaceRoot = fileURLToPath(new URL('../../..', import.meta.url))
  const themesDirectory = resolve(workspaceRoot, 'packages/design-tokens/src/themes')

  it('provides exactly three complete named themes', () => {
    const files = readdirSync(themesDirectory).filter((file) => file.endsWith('.css')).sort()
    expect(files).toEqual(['cloud-cobalt.css', 'graphite-signal.css', 'midnight-indigo.css'])
    for (const file of files) {
      const css = readFileSync(resolve(themesDirectory, file), 'utf8')
      for (const token of requiredTokens) expect(css, `${file} missing --${token}`).toMatch(new RegExp(`--${token}\\s*:`))
    }
  })

  it('keeps literal colors inside theme sources only', () => {
    const clientUiSource = resolve(workspaceRoot, 'packages/client-ui/src')
    const offenders: string[] = []
    const walk = (directory: string) => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = `${directory}/${entry.name}`
        if (entry.isDirectory()) walk(path)
        else if (/\.(vue|css|ts)$/.test(entry.name) && /#[\da-f]{3,8}|rgba?\(|hsla?\(/i.test(readFileSync(path, 'utf8'))) offenders.push(path)
      }
    }
    walk(clientUiSource)
    expect(offenders).toEqual([])
  })

  it('uses branded high-contrast QR colors instead of black and white defaults', () => {
    const parseHex = (value: string) => [1, 3, 5].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16))
    const luminance = (value: string) => parseHex(value)
      .map((channel) => channel / 255)
      .map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4)
      .reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0)
    const contrast = (first: string, second: string) => {
      const [lighter, darker] = [luminance(first), luminance(second)].sort((a, b) => b - a)
      return (lighter + 0.05) / (darker + 0.05)
    }
    const pairs = new Set<string>()

    for (const file of readdirSync(themesDirectory).filter((name) => name.endsWith('.css'))) {
      const css = readFileSync(resolve(themesDirectory, file), 'utf8')
      const foreground = css.match(/--color-qr-foreground:\s*(#[\da-f]{6})/i)?.[1].toLowerCase()
      const background = css.match(/--color-qr-background:\s*(#[\da-f]{6})/i)?.[1].toLowerCase()

      expect(foreground, `${file} QR foreground`).toBeTruthy()
      expect(background, `${file} QR background`).toBeTruthy()
      expect(foreground).not.toBe('#000000')
      expect(background).not.toBe('#ffffff')
      expect(new Set(parseHex(foreground!)).size, `${file} foreground should be branded`).toBeGreaterThan(1)
      expect(new Set(parseHex(background!)).size, `${file} background should be tinted`).toBeGreaterThan(1)
      expect(contrast(foreground!, background!), `${file} QR contrast`).toBeGreaterThanOrEqual(4.5)
      pairs.add(`${foreground}/${background}`)
    }

    expect(pairs.size).toBe(3)
  })
})
