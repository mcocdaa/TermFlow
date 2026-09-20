import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentCollapsibleOutput from './AgentCollapsibleOutput.vue'

describe('AgentCollapsibleOutput', () => {
  it('renders short content without folding controls', () => {
    const wrapper = mount(AgentCollapsibleOutput, {
      props: {
        content: 'line 1\nline 2\nline 3',
      },
    })
    expect(wrapper.find('[data-action="toggle-fold"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('line 1\nline 2\nline 3')
  })

  it('folds content exceeding 10 lines by default and can toggle expand/collapse', async () => {
    const lines = Array.from({ length: 25 }, (_, i) => `line ${i + 1}`).join('\n')
    const wrapper = mount(AgentCollapsibleOutput, {
      props: {
        content: lines,
      },
    })

    // Folded initially
    const button = wrapper.get('[data-action="toggle-fold"]')
    expect(button.text()).toContain('展开其余 17 行 (共 25 行)')
    expect(wrapper.text()).toContain('line 1')
    expect(wrapper.text()).not.toContain('line 10')
    expect(wrapper.text()).toContain('line 25')

    // Click to expand
    await button.trigger('click')
    expect(wrapper.text()).toContain('line 10')
    expect(wrapper.get('[data-action="toggle-fold"]').text()).toBe('收起输出 ▴')

    // Click to collapse
    await wrapper.get('[data-action="toggle-fold"]').trigger('click')
    expect(wrapper.get('[data-action="toggle-fold"]').text()).toContain('展开其余 17 行')
  })

  it('auto-expands long content when status is error or failed', () => {
    const lines = Array.from({ length: 20 }, (_, i) => `error line ${i + 1}`).join('\n')
    const wrapper = mount(AgentCollapsibleOutput, {
      props: {
        content: lines,
        status: 'error',
      },
    })

    // Should be expanded automatically
    expect(wrapper.get('[data-action="toggle-fold"]').text()).toBe('收起输出 ▴')
    expect(wrapper.text()).toContain('error line 10')
  })

  it('renders unified diff with structured highlight lines', () => {
    const diff = `--- a/file.ts
+++ b/file.ts
@@ -1,2 +1,3 @@
 context line
-old line
+new line
`
    const wrapper = mount(AgentCollapsibleOutput, {
      props: {
        content: diff,
      },
    })

    expect(wrapper.find('[data-agent-diff-viewer]').exists()).toBe(true)
    expect(wrapper.find('.agent-diff-line--add').text()).toBe('+new line')
    expect(wrapper.find('.agent-diff-line--del').text()).toBe('-old line')
    expect(wrapper.find('.agent-diff-line--hunk').text()).toContain('@@ -1,2 +1,3 @@')
  })
})
