import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import TerminalAgentToggle from './TerminalAgentToggle.vue'

describe('TerminalAgentToggle', () => {
  it('keeps readiness in the accessible label without rendering a titlebar status tag', () => {
    const wrapper = mount(TerminalAgentToggle, {
      props: { open: false, pendingCount: 0, readiness: 'ready' },
      global: { plugins: [createClientUi(createFakeRuntime())] },
    })

    const button = wrapper.get('[data-action="toggle-agent"]')
    expect(button.attributes('aria-label')).toContain('已就绪')
    expect(wrapper.find('[data-agent-readiness-badge]').exists()).toBe(false)
  })
})
