import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
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
  'shadow-panel', 'radius-sm', 'radius-md', 'radius-lg', 'space-1', 'space-2', 'space-3',
  'space-4', 'space-5', 'font-ui', 'font-mono',
]

describe('design token contract', () => {
  const themesDirectory = resolve(process.cwd(), '../../../packages/design-tokens/src/themes')

  it('provides exactly three complete named themes', () => {
    const files = readdirSync(themesDirectory).filter((file) => file.endsWith('.css')).sort()
    expect(files).toEqual(['cloud-cobalt.css', 'graphite-signal.css', 'midnight-indigo.css'])
    for (const file of files) {
      const css = readFileSync(resolve(themesDirectory, file), 'utf8')
      for (const token of requiredTokens) expect(css, `${file} missing --${token}`).toMatch(new RegExp(`--${token}\\s*:`))
    }
  })

  it('keeps literal colors inside theme sources only', () => {
    const webSource = resolve(process.cwd(), 'src')
    const offenders: string[] = []
    const walk = (directory: string) => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = `${directory}/${entry.name}`
        if (entry.isDirectory()) walk(path)
        else if (/\.(vue|css|ts)$/.test(entry.name) && /#[\da-f]{3,8}|rgba?\(|hsla?\(/i.test(readFileSync(path, 'utf8'))) offenders.push(path)
      }
    }
    walk(webSource)
    expect(offenders).toEqual([])
  })
})
