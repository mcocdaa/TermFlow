import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import App from '../App.vue'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import StatusPill from '../components/dashboard/StatusPill.vue'
import { createAppRouter } from '../router'

describe('accessibility contracts', () => {
  it('provides skip navigation, named navigation landmarks, and a focusable main target', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }), history: createMemoryHistory() })
    await router.push('/login')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    expect(wrapper.get('[href="#main-content"]')).toBeTruthy()
    expect(wrapper.get('aside[aria-label="主导航"]')).toBeTruthy()
    expect(wrapper.get('nav[aria-label="移动端导航"]')).toBeTruthy()
    expect(wrapper.get('main').attributes('tabindex')).toBe('-1')
  })

  it('traps modal focus and restores it to the invoking control', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%1', paneName: 'Shell' } })
    await wrapper.vm.$nextTick()
    expect(document.activeElement?.textContent).toContain('取消')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement?.textContent).toContain('取消')
    wrapper.unmount()
    expect(document.activeElement).toBe(invoker)
    invoker.remove()
  })

  it('closes the mobile action drawer with Escape and restores trigger focus', async () => {
    const wrapper = mount(TmuxActionMenu, { attachTo: document.body, props: { bindings: { prefix: 'C-a', bindings: [] }, activePaneId: '%1' } })
    const trigger = wrapper.get('[data-action="toggle-mobile-drawer"]')
    await trigger.trigger('click')
    await wrapper.get('[data-mobile-drawer]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('uses visible focus, reduced-motion guards, and status text independent of color', () => {
    const css = `${readFileSync(resolve(process.cwd(), 'src/styles/reset.css'), 'utf8')}\n${readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')}`
    expect(css).toContain(':focus-visible')
    expect(css).toContain('prefers-reduced-motion: no-preference')
    expect(mount(StatusPill, { props: { online: true } }).text()).toContain('在线')
    expect(mount(StatusPill, { props: { online: false } }).text()).toContain('离线')
  })
})
