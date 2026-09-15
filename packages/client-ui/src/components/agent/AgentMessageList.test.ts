import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { createAgentHistoryState } from '@termflow/client-core'
import AgentMessageList from './AgentMessageList.vue'

it('explains how to start when a conversation has no messages', () => {
  const wrapper = mount(AgentMessageList, {
    props: { history: createAgentHistoryState() },
  })

  expect(wrapper.get('[data-agent-message-empty]').text()).toContain('输入一条消息')
  wrapper.unmount()
})
