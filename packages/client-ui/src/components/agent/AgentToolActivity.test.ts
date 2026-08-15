import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AgentToolCallState } from '@termflow/client-core'
import AgentToolActivity from './AgentToolActivity.vue'

function call(overrides: Partial<AgentToolCallState> = {}): AgentToolCallState {
  return {
    toolCallId: 't1',
    toolName: 'ls',
    status: 'running',
    summary: null,
    startedAt: 0,
    endedAt: null,
    ...overrides,
  }
}

describe('AgentToolActivity', () => {
  it('renders running/completed/failed rows with Chinese status labels', () => {
    const running = mount(AgentToolActivity, { props: { call: call() } })
    expect(running.get('[data-agent-tool-status]').attributes('data-agent-tool-status')).toBe('running')
    expect(running.get('[data-agent-tool-status-label]').text()).toBe('运行中')
    running.unmount()

    const completed = mount(AgentToolActivity, { props: { call: call({ status: 'completed', summary: '{}' }) } })
    expect(completed.get('[data-agent-tool-status-label]').text()).toBe('已完成')
    completed.unmount()

    const failed = mount(AgentToolActivity, { props: { call: call({ status: 'failed', summary: '{"error_code":"E"}' }) } })
    expect(failed.get('[data-agent-tool-status-label]').text()).toBe('执行失败')
    failed.unmount()
  })

  it('toggles the summary with a native button exposing aria-expanded/aria-controls', async () => {
    const wrapper = mount(AgentToolActivity, { props: { call: call({ status: 'completed', summary: 'ok' }) } })
    const button = wrapper.get('button')
    expect(button.attributes('aria-expanded')).toBe('false')
    expect(button.attributes('aria-controls')).toBeTruthy()
    expect(wrapper.find('[data-agent-tool-summary]').exists()).toBe(false)

    await button.trigger('click')
    expect(wrapper.get('button').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('[data-agent-tool-summary]').text()).toBe('ok')

    await wrapper.get('button').trigger('click')
    expect(wrapper.get('button').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-agent-tool-summary]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders a static row without a toggle while the call is still running', () => {
    const wrapper = mount(AgentToolActivity, { props: { call: call() } })
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('[data-agent-tool-summary]').exists()).toBe(false)
    expect(wrapper.get('[data-agent-tool-status-label]').text()).toBe('运行中')
    wrapper.unmount()
  })

  it('renders HTML-shaped summaries literally — no element injection', async () => {
    const wrapper = mount(AgentToolActivity, {
      props: {
        call: call({ status: 'completed', summary: '<script>alert(1)</script><img src=x onerror=alert(2)> javascript:alert(3)' }),
      },
    })
    await wrapper.get('button').trigger('click')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.get('[data-agent-tool-summary]').text()).toBe(
      '<script>alert(1)</script><img src=x onerror=alert(2)> javascript:alert(3)',
    )
    wrapper.unmount()
  })

  it('strips ANSI/OSC sequences from the summary before rendering', async () => {
    const wrapper = mount(AgentToolActivity, {
      props: {
        call: call({ status: 'failed', summary: '\u001b[31m红色\u001b[0m \u001b]8;;javascript:alert(1)\u0007点击\u001b]8;;\u0007' }),
      },
    })
    await wrapper.get('button').trigger('click')
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.get('[data-agent-tool-summary]').text()).toBe('红色 点击')
    expect(wrapper.get('[data-agent-tool-summary]').html()).not.toContain('\u001b')
    wrapper.unmount()
  })

  it('labels an empty tool name and keeps pre-wrap summary rendering (CSS contract)', () => {
    const wrapper = mount(AgentToolActivity, { props: { call: call({ toolName: '', status: 'completed', summary: '{}' }) } })
    expect(wrapper.get('.agent-tool-activity__name').text()).toBe('工具调用')
    wrapper.unmount()

    const css = readFileSync(resolve(process.cwd(), 'src/styles/app.css'), 'utf8')
    expect(css).toMatch(/\.agent-tool-activity__summary-text\s*\{[^}]*white-space:\s*pre-wrap/s)
  })
})
