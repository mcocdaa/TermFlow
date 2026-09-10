import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import AgentToolActivity from './AgentToolActivity.vue'

it('uses the open-source Lucide chevron controls for expandable tool details', async () => {
  const wrapper = mount(AgentToolActivity, {
    props: {
      call: {
        toolCallId: 'tool-1',
        toolName: 'terminal.write',
        status: 'completed',
        summary: 'done',
        startedAt: 1,
        endedAt: 2,
      },
    },
  })

  expect(wrapper.find('.agent-tool-activity__chevron svg').exists()).toBe(true)
  await wrapper.get('[data-agent-tool-toggle]').trigger('click')
  expect(wrapper.find('.agent-tool-activity__chevron svg').exists()).toBe(true)
  expect(wrapper.get('[data-agent-tool-toggle]').attributes('aria-expanded')).toBe('true')
})
