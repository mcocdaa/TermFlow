//: localStorage-backed Agent cursor persistence for the browser (M6b spec
//: §4.4/§6.3). Stores only the opaque `{epoch}-{seq}` cursor and its seq
//: component — never message content. Reads validate shape and discard
//: anything invalid (treated as a cold start); persistence is an
//: optimization, correctness never depends on it.
import { isValidAgentCursor, type AgentCursorStore, type AgentCursorStoreEntry } from '@termflow/client-core'

const KEY_PREFIX = 'termflow.agent.cursor.'

type MinimalStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function createBrowserAgentCursorStore(storage: MinimalStorage = globalThis.localStorage): AgentCursorStore {
  return {
    load(conversationId: string): AgentCursorStoreEntry | null {
      const raw = storage.getItem(`${KEY_PREFIX}${conversationId}`)
      if (raw === null) return null
      try {
        const parsed: unknown = JSON.parse(raw)
        if (!record(parsed)) return null
        if (typeof parsed.cursor !== 'string' || !isValidAgentCursor(parsed.cursor)) return null
        if (typeof parsed.seq !== 'number' || !Number.isSafeInteger(parsed.seq) || parsed.seq < 0) return null
        return { cursor: parsed.cursor, seq: parsed.seq }
      } catch {
        // Unparseable or hostile JSON: discard and cold-start.
        return null
      }
    },
    save(conversationId: string, cursor: string, seq: number): void {
      // Validate before write (M6b spec §5): never persist a cursor the
      // session could not restore.
      if (!isValidAgentCursor(cursor) || !Number.isSafeInteger(seq) || seq < 0) return
      storage.setItem(`${KEY_PREFIX}${conversationId}`, JSON.stringify({ cursor, seq }))
    },
    clear(conversationId: string): void {
      storage.removeItem(`${KEY_PREFIX}${conversationId}`)
    },
  }
}
