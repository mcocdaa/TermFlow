import { describe, expect, it, vi } from 'vitest'
import { createBrowserClipboard } from './browserClipboard'

describe('browser clipboard adapter', () => {
  it('delegates text writes to the injected browser clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const clipboard = createBrowserClipboard({ writeText })

    await clipboard.writeText('termflow login --code SAFE')

    expect(writeText).toHaveBeenCalledWith('termflow login --code SAFE')
  })
})
