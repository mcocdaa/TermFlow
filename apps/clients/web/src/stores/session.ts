import { createSessionActions, sessionState } from '@termflow/client-ui'
import { browserRuntime } from '../runtime'

const actions = createSessionActions(browserRuntime.api)

export { sessionState }
export const refreshSession = actions.refreshSession
export const loginWithToken = actions.loginWithToken
export const logoutSession = actions.logoutSession
