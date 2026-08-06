import { describe, expect, it } from 'vitest'
import { createAuthorizationStateMachine, type AuthorizationState } from './authorizationState'

describe('createAuthorizationStateMachine', () => {
  it('reports the browser and device authorization progress in a stable order', () => {
    const states: AuthorizationState[] = []
    const session = createAuthorizationStateMachine({ onState: (state) => states.push(state) })

    session.requesting()
    session.pending()
    session.approved()
    session.connected()

    expect(states).toEqual(['requesting', 'pending', 'approved', 'connected'])
    expect(session.current()).toBe('connected')
  })

  it('reports cancellation as a terminal state only once', () => {
    const states: AuthorizationState[] = []
    const session = createAuthorizationStateMachine({ onState: (state) => states.push(state) })

    session.pending()
    session.cancelled()
    session.failed()

    expect(states).toEqual(['pending', 'cancelled'])
  })
})
