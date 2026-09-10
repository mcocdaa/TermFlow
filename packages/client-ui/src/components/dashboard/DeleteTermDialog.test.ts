import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import DeleteTermDialog from './DeleteTermDialog.vue'

it('keeps the cleanup job visible without completed wording', () => {
  const wrapper = mount(DeleteTermDialog, { props: { term: { name: 'test', instance_id: 'term-1' } as never,
    pending: false, error: '', cleanupJobId: 'job-1' } })
  expect(wrapper.get('[role="status"]').text()).toContain('job-1')
  expect(wrapper.text()).toContain('清理待完成')
  expect(wrapper.get('[data-action="confirm-delete-term"]').attributes('disabled')).toBeDefined()
  wrapper.unmount()
})
