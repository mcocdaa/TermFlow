export type AuthorizationState =
  | 'requesting'
  | 'pending'
  | 'approved'
  | 'connected'
  | 'cancelled'
  | 'failed'

export interface AuthorizationStateMachineOptions {
  onState?: ((state: AuthorizationState) => void) | undefined
}

const terminalStates = new Set<AuthorizationState>(['connected', 'cancelled', 'failed'])

export function createAuthorizationStateMachine(options: AuthorizationStateMachineOptions = {}) {
  let state: AuthorizationState | undefined

  function transition(next: AuthorizationState): void {
    if (state !== undefined && terminalStates.has(state)) return
    state = next
    options.onState?.(next)
  }

  return {
    current: () => state,
    requesting: () => transition('requesting'),
    pending: () => transition('pending'),
    approved: () => transition('approved'),
    connected: () => transition('connected'),
    cancelled: () => transition('cancelled'),
    failed: () => transition('failed'),
  }
}
