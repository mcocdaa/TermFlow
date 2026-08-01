import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import EnrollmentDialog from './EnrollmentDialog.vue'

describe('EnrollmentDialog', () => {
  it('creates a one-time code only on request, builds the login command, copies it, and clears on close', async () => {
    const code = 'JOIN-7P4W-SECRET'
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code, expires_at: new Date(Date.now() + 600_000).toISOString() }), { status: 201, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const wrapper = mount(EnrollmentDialog)

    expect(fetchMock).not.toHaveBeenCalled()
    await wrapper.get('[data-action="create-code"]').trigger('click')
    await flushPromises()

    const command = `termflow login --server ${window.location.origin} --code ${code}`
    expect(wrapper.text()).toContain(code)
    expect(wrapper.text()).toContain(command)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    await wrapper.get('[data-action="copy-command"]').trigger('click')
    expect(writeText).toHaveBeenCalledWith(command)
    await wrapper.get('[data-action="close-enrollment"]').trigger('click')
    expect(wrapper.html()).not.toContain(code)
  })

  it('clears an expired code from the DOM', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-01T00:00:00Z'))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'EXPIRES-NOW', expires_at: '2026-08-01T00:00:01Z' }), { status: 201, headers: { 'content-type': 'application/json' } })))
    const wrapper = mount(EnrollmentDialog)
    await wrapper.get('[data-action="create-code"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('EXPIRES-NOW')
    await vi.advanceTimersByTimeAsync(1_100)
    expect(wrapper.html()).not.toContain('EXPIRES-NOW')
    vi.useRealTimers()
  })
})
