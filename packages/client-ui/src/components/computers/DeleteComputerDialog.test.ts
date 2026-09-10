import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import DeleteComputerDialog from './DeleteComputerDialog.vue'

it('keeps the cleanup job visible without completed wording', () => {
  const wrapper = mount(DeleteComputerDialog, { props: { computer: { display_name: 'test', installation_id: 'computer-1' } as never,
    pending: false, error: '', cleanupJobId: 'job-1' } })
  expect(wrapper.get('[role="status"]').text()).toContain('job-1')
  expect(wrapper.text()).toContain('清理待完成')
  expect(wrapper.get('[data-action="confirm-delete-computer"]').attributes('disabled')).toBeDefined()
  wrapper.unmount()
})
