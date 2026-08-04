import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it } from 'vitest'
import App from '../App.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import { clientRoutes } from '../router/routes'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from './fakeRuntime'

const viewports = [[360, 800], [800, 360], [1024, 768], [1440, 900]] as const

describe('responsive shell contract', () => {
  it.each(viewports)('keeps navigation and terminal title controls reachable at %ix%i', async (width, height) => {
    Object.defineProperties(window, { innerWidth: { value: width, configurable: true }, innerHeight: { value: height, configurable: true } })
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/')
    await router.isReady()
    const app = mount(App, { global: { plugins: [router, createClientUi(createFakeRuntime())] } })
    expect(app.get('main')).toBeTruthy()
    expect(app.get('.side-nav')).toBeTruthy()
    expect(app.get('.mobile-nav')).toBeTruthy()
    const titlebar = mount(TerminalTitlebar, {
      props: { title: 'Term', displayMode: 'font-100' },
      global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
    })
    expect(titlebar.get('.terminal-titlebar')).toBeTruthy()
    expect(titlebar.get('[data-action="toggle-display-menu"]')).toBeTruthy()
    const viewportLock = titlebar.get('[data-action="toggle-viewport-lock"]')
    expect(viewportLock.attributes('aria-pressed')).toBe('false')
    const tmux = mount(TmuxActionMenu, { props: { bindings: { prefix: 'C-a', bindings: [] }, activePaneId: '%1' } })
    expect(tmux.get('[data-action="toggle-tmux-menu"]')).toBeTruthy()
    expect(tmux.find('[data-action="toggle-mobile-drawer"]').exists()).toBe(false)
    expect(tmux.find('[data-mobile-drawer]').exists()).toBe(false)
    tmux.unmount()
    titlebar.unmount()
    app.unmount()
  })

  it('uses one portrait/landscape logic with coarse-pointer behavior and safe-area insets', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/terminal-responsive.css'), 'utf8')
    expect(css).toContain('(pointer: coarse)')
    expect(css).not.toContain('orientation:')
    expect(css).toContain('safe-area-inset-bottom')
    expect(css).toContain('touch-action: none')
    expect(css).toContain('touch-action: pan-x')
    expect(css).toContain('.mobile-keybar-shell {')
    expect(css).toContain('grid-row: 3;')
    expect(css).toContain('overflow: hidden;')
    expect(css).toContain('.mobile-keybar {')
    expect(css).toContain('overscroll-behavior-x: none;')
    expect(css).toContain('overscroll-behavior-y: none;')
    expect(css).toMatch(
      /\.terminal-frame,\s*\.mobile-keybar\s*\{\s*scrollbar-width: none;\s*\}/,
    )
    expect(css).toMatch(
      /\.terminal-frame::\-webkit-scrollbar,\s*\.mobile-keybar::\-webkit-scrollbar\s*\{\s*display: none;\s*\}/,
    )
    expect(css).toContain('overflow-x: auto;')
    expect(css).toContain('touch-action: pan-x;')
    expect(css).not.toContain('overscroll-behavior-inline: contain;')
    expect(css).not.toContain('overscroll-behavior-block: none;')
    expect(css).toContain('padding-block-end: max(var(--space-2), env(safe-area-inset-bottom))')
    expect(css).not.toContain('.mobile-keybar-shell { position: fixed')
    expect(css).not.toContain('inset-block-end: env(safe-area-inset-bottom)')
    expect(css).toContain('.control-label, .menu-chevron, .terminal-identifiers [data-computer-name] { display: none; }')
    expect(css).toContain('.terminal-view { height: 100dvh; grid-template-rows: auto minmax(0, 1fr) auto; }')
    expect(css).not.toContain('.mobile-action-trigger')
    expect(css).not.toContain('.mobile-action-drawer')
    expect(css).toContain('.terminal-status { position: absolute;')
    const appCss = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    const resetCss = readFileSync(resolve(process.cwd(), 'src/styles/reset.css'), 'utf8')
    expect(appCss).toContain(".computer-table-head, .computer-table-row { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));")
    expect(appCss).toContain('.computer-table-head > :not(:first-child) { justify-self: center; text-align: center; }')
    expect(appCss).toContain(".computer-table-row [role='cell']:not(:first-child) { align-items: center; text-align: center; }")
    expect(appCss).toContain('.computer-table-actions { align-items: center; }')
    expect(appCss).toMatch(/@media \(max-width: 64rem\)[\s\S]*\.computer-table-row \[role='cell'\]:not\(:first-child\) \{[^}]*align-items: flex-start;[^}]*text-align: start;/)
    expect(resetCss).toContain('html, body, #app { width: 100%; height: 100dvh; min-height: 0; margin: 0; overflow: hidden; }')
    expect(resetCss).toContain('html.termflow-terminal-route,')
    expect(resetCss).toContain('body.termflow-terminal-route { position: fixed; inset: 0; }')
    expect(appCss).not.toContain('.mobile-action-trigger')
    expect(appCss).not.toContain('.mobile-action-drawer')
    expect(appCss).not.toContain('scrollbar-width: none;')
    expect(appCss).toContain('.mobile-keybar-shell, .mobile-keybar { display: none; }')
    expect(appCss).not.toContain('height: calc(100% - 3.25rem)')
    expect(appCss).toContain(".app-shell { display: grid; height: 100dvh; min-height: 0; overflow: hidden; grid-template: auto minmax(0, 1fr) / 15rem minmax(0, 1fr);")
    expect(appCss).toContain('main { grid-area: main; min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain;')
    expect(appCss).toContain('.app-shell.is-bare { display: block; height: 100dvh; min-height: 0; overflow-y: auto; }')
    expect(appCss).toContain('.totp-guide-card { min-height: 22rem; align-content: start; gap: var(--space-5); }')
    expect(appCss).toContain('.totp-guide-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));')
    expect(appCss).toContain('.totp-bind-layout { width: min(100%, 46rem); margin-inline: auto; display: grid;')
    expect(appCss).toMatch(/@media \(max-width: 47\.99rem\)[\s\S]*\.totp-bind-layout \{[^}]*grid-template-columns: 1fr;/)
    expect(appCss).toContain('.setup-key-disclosure { position: relative;')
    expect(appCss).toContain('.setup-key-popover { position: absolute;')
    expect(appCss).toContain('width: min(18rem, calc(100vw - 2 * var(--space-4)));')
    expect(appCss).toContain('z-index: 20;')
    expect(appCss).not.toContain('.setup-key-panel')
    expect(appCss).not.toContain('.totp-guide-steps { grid-template-columns: 1fr; }')
    expect(appCss).toContain('.security-setting-row { position: relative;')
    expect(appCss).toContain('.security-setting-row > .security-setting-label { position: static;')
    expect(appCss).toContain(".security-setting-label .help-tooltip [role='tooltip'] { inset-inline: var(--space-3); width: auto; max-width: none; white-space: normal; }")
    expect(appCss).toContain('.settings-panel-heading { position: relative;')
    expect(appCss).toContain(".settings-panel-heading .help-tooltip [role='tooltip'] { inset-inline: 0; width: auto; white-space: normal; }")
    expect(appCss).toContain('.qr-dialog-panel > p { margin: 0; color: var(--color-text-secondary); line-height: 1.6; }')
    expect(appCss).toContain('.app-shell.is-terminal { display: block; height: 100dvh; min-height: 0; overflow: hidden; }')
    expect(appCss).toContain('.terminal-view { position: relative; max-width: none; height: 100%; min-height: 0; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }')
    expect(appCss).toContain('.terminal-identifiers { min-width: 0; display: flex; align-items: center; gap: var(--space-2); white-space: nowrap; }')
    expect(appCss).toContain(".terminal-frame[data-display-mode='fit'] { overflow: hidden; }")
    expect(appCss).toContain(".terminal-frame[data-viewport-lock='locked'] { overflow: hidden; }")
    expect(appCss).toContain(".viewport-lock-button[aria-pressed='true'] {")
    expect(appCss).toContain(".titlebar-button[aria-expanded='true'] {")
    expect(appCss).toContain('@media (hover: hover) and (pointer: fine) {')
    expect(appCss).not.toContain('.titlebar-button:focus-visible')
    expect(css).not.toContain(".viewport-lock-button[aria-pressed='true']")
    expect(css).toContain('.titlebar-menu { position: static;')
    expect(css).toContain('.terminal-titlebar { z-index: 50;')
    expect(css).toContain('.terminal-titlebar .floating-menu {')
    expect(css).toContain('max-height: calc(100dvh - 3.25rem - 2 * var(--space-2));')
    expect(appCss).toContain(".terminal-frame[data-display-mode='fit'] .xterm-viewport { overflow-y: hidden !important; }")
    const terminalCanvas = readFileSync(resolve(process.cwd(), 'src/components/terminal/TerminalCanvas.vue'), 'utf8')
    expect(terminalCanvas).toContain('session.setVisualScale(requested)')
    expect(terminalCanvas).not.toContain('scale(${')
    expect(appCss).toContain('.term-counts { grid-row: 2; grid-column: 1 / -1; }')
    expect(appCss).toContain('.term-last-seen { grid-row: 3; grid-column: 1 / -1; }')
  })
})
