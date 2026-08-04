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
    const appCss = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')

    expect(css).toContain('(pointer: coarse)')
    expect(css).not.toContain('orientation:')
    expect(css).toContain('safe-area-inset-bottom')
    expect(css).toContain('touch-action: none')
    expect(css).toContain('touch-action: pan-x')
    expect(css).toMatch(/\.mobile-keybar-shell\s*\{[^}]*grid-row: 3;[^}]*overflow: hidden;[^}]*safe-area-inset-bottom/s)
    expect(css).toMatch(/\.mobile-keybar\s*\{[^}]*overflow-x: auto;[^}]*overscroll-behavior-x: none;[^}]*overscroll-behavior-y: none;/s)
    expect(css).toMatch(/\.terminal-frame,\s*\.mobile-keybar\s*\{[^}]*scrollbar-width: none;/s)
    expect(css).toMatch(/\.terminal-frame::\-webkit-scrollbar,\s*\.mobile-keybar::\-webkit-scrollbar\s*\{[^}]*display: none;/s)
    expect(css).not.toContain('overscroll-behavior-inline: contain;')
    expect(css).not.toContain('overscroll-behavior-block: none;')
    expect(css).not.toMatch(/\.mobile-keybar-shell\s*\{[^}]*position: fixed/s)

    expect(appCss).toMatch(/\.computer-table-head,\s*\.computer-table-row\s*\{[^}]*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\);/s)
    expect(appCss).toMatch(/\.computer-table-head > :not\(:first-child\)\s*\{[^}]*justify-self: center;[^}]*text-align: center;/s)
    expect(appCss).toMatch(/\.computer-table-row \[role='cell'\]:not\(:first-child\)\s*\{[^}]*align-items: center;[^}]*text-align: center;/s)
    expect(appCss).toMatch(/\.computer-table-actions\s*\{[^}]*align-items: center;/s)
    expect(appCss).toMatch(/\.computer-delete-toast\s*\{[^}]*position: fixed;[^}]*safe-area-inset-bottom/s)
    expect(appCss).toMatch(/@media \(max-width: 47\.99rem\)[\s\S]*\.computer-delete-toast\s*\{[^}]*inset-block-end: calc\(5rem \+ env\(safe-area-inset-bottom\)\);/)
  })
})
