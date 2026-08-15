import { describe, expect, it } from 'vitest'
import { createBrowserAgentCursorStore } from './browserAgentCursorStore'

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key) },
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

describe('createBrowserAgentCursorStore', () => {
  it('round-trips a saved cursor under the per-conversation key', () => {
    const storage = memoryStorage()
    const store = createBrowserAgentCursorStore(storage)

    store.save('conv-1', '7-42', 42)
    expect(store.load('conv-1')).toEqual({ cursor: '7-42', seq: 42 })
    expect(storage.getItem('termflow.agent.cursor.conv-1')).toBe('{"cursor":"7-42","seq":42}')
    // Conversations are isolated by key.
    expect(store.load('conv-2')).toBeNull()
    expect(store.load('conv-1x')).toBeNull()
  })

  it.each([
    ['broken JSON', '{not json'],
    ['non-object JSON', '"7-1"'],
    ['missing seq', '{"cursor":"7-1"}'],
    ['wrong types', '{"cursor":7,"seq":1}'],
    ['non-cursor string', '{"cursor":"garbage","seq":1}'],
    ['epoch zero cursor', '{"cursor":"0-1","seq":1}'],
    ['negative seq', '{"cursor":"7-1","seq":-1}'],
    ['non-integer seq', '{"cursor":"7-1","seq":1.5}'],
    ['seq as string', '{"cursor":"7-1","seq":"1"}'],
  ])('discards invalid persisted values (%s) as a cold start', (_label, raw) => {
    const store = createBrowserAgentCursorStore(memoryStorage({ 'termflow.agent.cursor.conv-1': raw }))
    expect(store.load('conv-1')).toBeNull()
  })

  it('validates before write: invalid cursors or seqs are never persisted', () => {
    const storage = memoryStorage()
    const store = createBrowserAgentCursorStore(storage)

    store.save('conv-1', 'not-a-cursor', 1)
    store.save('conv-1', '7-1', -1)
    store.save('conv-1', '7-1', 1.5)
    store.save('conv-1', '0-1', 1)
    expect(storage.getItem('termflow.agent.cursor.conv-1')).toBeNull()
  })

  it('clear removes only the target conversation entry', () => {
    const storage = memoryStorage({
      'termflow.agent.cursor.conv-1': '{"cursor":"7-1","seq":1}',
      'termflow.agent.cursor.conv-2': '{"cursor":"7-2","seq":2}',
      'unrelated.key': 'keep-me',
    })
    const store = createBrowserAgentCursorStore(storage)

    store.clear('conv-1')
    expect(store.load('conv-1')).toBeNull()
    expect(store.load('conv-2')).toEqual({ cursor: '7-2', seq: 2 })
    expect(storage.getItem('unrelated.key')).toBe('keep-me')
  })
})
