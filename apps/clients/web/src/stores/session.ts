import { createSessionActions, sessionState } from '@termflow/client-ui'
import { browserApiClient } from '../api/http'

const actions = createSessionActions(browserApiClient)

export { sessionState }
export const refreshSession = actions.refreshSession
export const loginWithToken = actions.loginWithToken
export const logoutSession = actions.logoutSession
