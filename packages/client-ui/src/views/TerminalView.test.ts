import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, onUnmounted, ref } from 'vue'
import { useTerminalSession } from '../composables/useTerminalSession'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import TerminalView from './TerminalView.vue'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
const created = vi.fn(), disposed = vi.fn(), panelCreated = vi.fn(), panelDisposed = vi.fn()
const Canvas = defineComponent({ props: ['termId'], setup(props) { created(); useTerminalSession(props.termId, ref(null)); onUnmounted(disposed); return { resetViewport() {}, captureViewport() {}, restoreViewport() {} } }, template: '<div data-canvas />' })
const AgentPanel = defineComponent({
  props: ['conversationId', 'open', 'mobilePage'],
  emits: ['close', 'selectConversation', 'pendingCount', 'readiness'],
  setup() { panelCreated(); onUnmounted(panelDisposed); return {} },
  template: `<aside v-show="open" :aria-hidden="open ? undefined : 'true'" :data-mobile-page="mobilePage ? 'true' : 'false'" data-agent-panel-stub>
    <button data-close @click="$emit('close')">关闭</button>
    <button data-select @click="$emit('selectConversation', '11111111-1111-4111-8111-111111111111')">会话</button>
    <button data-panel-status @click="$emit('readiness', 'ready'); $emit('pendingCount', 2)">状态</button>
    <button data-panel-background @click="$emit('pendingCount', 3)">后台状态</button>
  </aside>`,
})
afterEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
function installOverlayMedia(initialMatches: boolean) {
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const media = '(max-width: 47.99rem), (pointer: coarse)'
  const query = {
    get matches() { return matches },
    media,
    onchange: null,
    addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => listeners.add(listener as (event: MediaQueryListEvent) => void),
    removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => listeners.delete(listener as (event: MediaQueryListEvent) => void),
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => true,
  } as MediaQueryList
  vi.mocked(window.matchMedia).mockImplementation((value) => {
    expect(value).toBe(media)
    return query
  })
  return {
    setMatches(value: boolean) {
      matches = value
      const event = { matches, media } as MediaQueryListEvent
      for (const listener of listeners) listener(event)
    },
  }
}
async function harness(enabled = true, initialRoute = '/term/t1?keep=yes', overlayMatches = window.innerWidth < 768) {
  const overlayMedia = installOverlayMedia(overlayMatches)
  const runtime = createFakeRuntime()
  const terminalFactory = vi.fn(runtime.createTerminal)
  const configuredRuntime = { ...runtime, createTerminal: terminalFactory }
  runtime.api.agents.capabilities = async () => ({ agent_broker_enabled: enabled, delegated_write_grants_enabled: false, state: enabled ? 'ready' : 'disabled', reason_code: null })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }, { path: '/term/:termId', component: TerminalView }] })
  await router.push(initialRoute)
  const w = mount(TerminalView, { attachTo: document.body, global: { plugins: [createClientUi(configuredRuntime), router], stubs: { TerminalCanvas: Canvas, TerminalAgentPanel: AgentPanel } } })
  await flushPromises()
  return { w, router, terminalFactory, overlayMedia }
}
it('opens sidecar and changes only the agent query without recreating terminal', async () => {
  const { w, router, terminalFactory } = await harness()
  await w.get('[data-action="toggle-agent"]').trigger('click'); await flushPromises()
  await w.get('[data-select]').trigger('click'); await flushPromises()
  expect(router.currentRoute.value.path).toBe('/term/t1')
  expect(router.currentRoute.value.query).toEqual({ agent: '11111111-1111-4111-8111-111111111111' })
  expect(created).toHaveBeenCalledOnce(); expect(disposed).not.toHaveBeenCalled()
  expect(terminalFactory).toHaveBeenCalledOnce()
  await w.get('[data-close]').trigger('click'); await flushPromises()
  expect(router.currentRoute.value.query).toEqual({})
  expect(document.activeElement).toBe(w.get('[data-action="toggle-agent"]').element)
  expect(created).toHaveBeenCalledOnce()
  w.unmount()
})

it('keeps one hidden sidecar owner and exposes readiness through the accessible toggle label', async () => {
  const { w } = await harness(true, '/term/t1', false)
  await w.get('[data-action="toggle-agent"]').trigger('click')
  await w.get('[data-panel-status]').trigger('click')
  expect(w.get('[data-action="toggle-agent"]').attributes('aria-label')).toContain('已就绪')
  expect(w.find('[data-agent-readiness-badge]').exists()).toBe(false)
  expect(w.get('[data-agent-pending-badge]').text()).toBe('2')

  await w.get('[data-close]').trigger('click')
  await flushPromises()
  expect(w.get('[data-agent-panel-stub]').attributes('aria-hidden')).toBe('true')
  expect(w.get('[data-action="toggle-agent"]').attributes('aria-label')).toContain('已就绪')
  expect(w.find('[data-agent-readiness-badge]').exists()).toBe(false)
  expect(w.get('[data-agent-pending-badge]').text()).toBe('2')
  expect(panelDisposed).not.toHaveBeenCalled()

  await w.get('[data-panel-background]').trigger('click')
  expect(w.get('[data-agent-pending-badge]').text()).toBe('3')
  await w.get('[data-action="toggle-agent"]').trigger('click')
  expect(panelCreated).toHaveBeenCalledOnce()
  w.unmount()
})
it('keeps a mobile terminal interactive when a deep link targets a disabled broker', async () => {
  window.innerWidth = 500
  const { w } = await harness(false, '/term/t1?agent=11111111-1111-4111-8111-111111111111')
  expect(w.find('[data-action="toggle-agent"]').exists()).toBe(false)
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeUndefined()
  w.unmount(); window.innerWidth = 1024
})
it('makes canvas and mobile key bar inert only in mobile overlay mode', async () => {
  window.innerWidth = 1200
  const { w, overlayMedia } = await harness()
  await w.get('[data-action="toggle-agent"]').trigger('click')
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeUndefined()
  window.innerWidth = 500; overlayMedia.setMatches(true); await flushPromises()
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeDefined()
  expect(w.find('[aria-modal="true"]').exists()).toBe(false)
  await w.get('[data-close]').trigger('click'); await flushPromises()
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeUndefined()
  w.unmount(); window.innerWidth = 1024
})

it.each([
  { label: '390px portrait', width: 390, overlayMatches: true, inert: true },
  { label: '844px coarse landscape', width: 844, overlayMatches: true, inert: true },
  { label: '844px fine desktop', width: 844, overlayMatches: false, inert: false },
])('uses the shared media contract for $label', async ({ width, overlayMatches, inert }) => {
  window.innerWidth = width
  const { w } = await harness(true, '/term/t1', overlayMatches)
  await w.get('[data-action="toggle-agent"]').trigger('click')
  expect(w.get('[data-terminal-interaction-host]').attributes('inert') !== undefined).toBe(inert)
  expect(w.get('[data-agent-panel-stub]').attributes('data-mobile-page')).toBe(overlayMatches ? 'true' : 'false')
  w.unmount(); window.innerWidth = 1024
})

it('updates inert state when the shared media query changes', async () => {
  window.innerWidth = 844
  const { w, overlayMedia } = await harness(true, '/term/t1', false)
  await w.get('[data-action="toggle-agent"]').trigger('click')
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeUndefined()
  overlayMedia.setMatches(true)
  await flushPromises()
  expect(w.get('[data-terminal-interaction-host]').attributes('inert')).toBeDefined()
  w.unmount(); window.innerWidth = 1024
})

it('makes the Agent panel fill the workspace in the mobile media block', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/styles/terminal-responsive.css'), 'utf8')
  const mobileBlock = css.match(/@media \(max-width: 47\.99rem\), \(pointer: coarse\) \{([\s\S]*?)\n  \}/)?.[1]
  expect(mobileBlock).toContain('.terminal-agent-panel--mobile-page')
  expect(mobileBlock).toContain('inset: 0')
  expect(mobileBlock).toContain('width: 100%')
  expect(mobileBlock).toContain('height: 100%')
})
