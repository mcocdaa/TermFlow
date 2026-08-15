//: Opaque Agent cursor parsing shared by the stream session and the cursor
//: persistence port (M6b spec §4.3). The rules mirror the internal
//: validation in frames.ts (`{epoch}-{seq}`, epoch >= 1, seq >= 0) so the
//: live-frame seq component and persisted cursors agree on one shape.
//: frames.ts itself is intentionally untouched.
const CURSOR_SHAPE = /^[1-9][0-9]*-[0-9]+$/

/**
 * Extract the sequence component of an opaque ``{epoch}-{seq}`` cursor.
 *
 * Returns the seq as a non-negative integer, or ``null`` when the cursor is
 * not a valid opaque Agent cursor. The seq (never the epoch) is what the
 * session deduplicates against: AG-UI events carry no `database_seq`, so the
 * envelope cursor is the single source of sequencing (M6b spec §4.3).
 */
export function parseCursorSeq(cursor: string): number | null {
  if (!CURSOR_SHAPE.test(cursor)) return null
  const seq = Number(cursor.slice(cursor.indexOf('-') + 1))
  return Number.isSafeInteger(seq) && seq >= 0 ? seq : null
}

/**
 * Validate the opaque ``{epoch}-{seq}`` cursor shape (epoch >= 1, seq >= 0).
 * Persisted cursors that fail this check are discarded and treated as a cold
 * start; correctness never depends on persistence (M6b spec §4.4).
 */
export function isValidAgentCursor(cursor: string): boolean {
  return CURSOR_SHAPE.test(cursor)
}

/** One validated persisted cursor entry (M6b spec §4.4). */
export interface AgentCursorStoreEntry {
  cursor: string
  seq: number
}

/**
 * Cursor persistence port. The client-core package never touches browser
 * storage itself; a platform adapter (web: browser storage keyed
 * ``termflow.agent.cursor.<conversationId>``; Tauri: its own store) implements
 * this port and the UI runtime injects it (M6b spec §4.6).
 *
 * The stored value holds only the opaque cursor and its seq component, never
 * message content (M6b spec §6.3).
 */
export interface AgentCursorStore {
  /** Restore a previously saved cursor, or ``null`` for a cold start. */
  load(conversationId: string): AgentCursorStoreEntry | null
  save(conversationId: string, cursor: string, seq: number): void
  clear(conversationId: string): void
}
