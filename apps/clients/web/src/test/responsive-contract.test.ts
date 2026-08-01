import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import App from '../App.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import { createAppRouter } from '../router'

const viewports = [[360, 800], [800, 360], [1024, 768], [1440, 900]] as const

describe('responsive shell contract', () => {
  it.each(viewports)('keeps navigation and terminal title controls reachable at %ix%i', async (width, height) => {
    Object.defineProperties(window, { innerWidth: { value: width, configurable: true }, innerHeight: { value: height, configurable: true } })
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/')
    await router.isReady()
    const app = mount(App, { global: { plugins: [router] } })
    expect(app.get('main')).toBeTruthy()
    expect(app.get('.side-nav')).toBeTruthy()
    expect(app.get('.mobile-nav')).toBeTruthy()
    const titlebar = mount(TerminalTitlebar, {
      props: { title: 'Term', displayMode: 'font-100' },
      global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
    })
    expect(titlebar.get('.terminal-titlebar')).toBeTruthy()
    expect(titlebar.get('[data-action="toggle-display-menu"]')).toBeTruthy()
    const tmux = mount(TmuxActionMenu, { props: { bindings: { prefix: 'C-a', bindings: [] }, activePaneId: '%1' } })
    expect(tmux.get('[data-action="toggle-mobile-drawer"]')).toBeTruthy()
    expect(tmux.find('[data-mobile-drawer]').exists()).toBe(false)
  })

  it('uses one portrait/landscape logic with coarse-pointer behavior and safe-area insets', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/terminal-responsive.css'), 'utf8')
    expect(css).toContain('(pointer: coarse)')
    expect(css).not.toContain('orientation:')
    expect(css).toContain('safe-area-inset-bottom')
    expect(css).toContain('touch-action: none')
    expect(css).toContain('.mobile-keybar { position: fixed; z-index: 40; inset-inline: 0; inset-block-end: env(safe-area-inset-bottom)')
    expect(css).toContain('.titlebar-button { padding-inline: var(--space-2); white-space: nowrap; }')
    expect(css).toContain('.terminal-status { position: absolute;')
    const appCss = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    expect(appCss).not.toContain('height: calc(100% - 3.25rem)')
    expect(appCss).toContain('.app-shell.is-terminal { display: block; height: 100dvh; min-height: 0; overflow: hidden; }')
    expect(appCss).toContain('.terminal-view { position: relative; max-width: none; height: 100%; min-height: 0; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }')
    expect(appCss).toContain('.terminal-identifiers { min-width: 0; display: flex; align-items: center; gap: var(--space-2); white-space: nowrap; }')
    expect(appCss).toContain(".terminal-frame[data-display-mode='fit'] { overflow: hidden; }")
    expect(appCss).toContain(".terminal-frame[data-display-mode='fit'] .xterm-viewport { overflow-y: hidden !important; }")
    expect(appCss).toContain('.term-counts { grid-row: 2; grid-column: 1 / -1; }')
    expect(appCss).toContain('.term-last-seen { grid-row: 3; grid-column: 1 / -1; }')
  })
})
