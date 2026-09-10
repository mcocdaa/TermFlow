import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AgentComposer from './AgentComposer.vue'

it('renders the send action as an accessible open-source icon button', () => {
  const wrapper = mount(AgentComposer, {
    props: { conversationId: 'c1', backendState: 'ready' },
    global: { plugins: [createClientUi(createFakeRuntime())] },
  })
  const send = wrapper.get('[data-action="send-message"]')
  expect(send.attributes('aria-label')).toBe('发送消息')
  expect(send.find('svg').exists()).toBe(true)
  expect(wrapper.find('[data-agent-composer-input-shell]').exists()).toBe(true)
  wrapper.unmount()
})
