import type { LocationQuery, LocationQueryRaw } from 'vue-router'

/** Terminal sidecar deep links intentionally carry one public query key. */
export const agentConversationIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export function isAgentConversationId(value: unknown): value is string {
  return typeof value === 'string' && agentConversationIdPattern.test(value)
}

export function canonicalTerminalQuery(query: LocationQuery | LocationQueryRaw): LocationQueryRaw {
  return isAgentConversationId(query.agent) ? { agent: query.agent } : {}
}

export function isCanonicalTerminalQuery(query: LocationQuery | LocationQueryRaw): boolean {
  const canonical = canonicalTerminalQuery(query)
  const keys = Object.keys(query)
  const canonicalKeys = Object.keys(canonical)
  return keys.length === canonicalKeys.length
    && canonicalKeys.every((key) => query[key] === canonical[key])
}
