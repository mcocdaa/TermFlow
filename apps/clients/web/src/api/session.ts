import type { SessionStatusDto } from './types'
import { browserApiClient } from './http'

export const getSessionStatus = (signal?: AbortSignal): Promise<SessionStatusDto> => browserApiClient.sessions.status(signal)
export const createSession = (adminToken: string, signal?: AbortSignal): Promise<SessionStatusDto> => browserApiClient.sessions.login(adminToken, signal)
export const deleteSession = async (signal?: AbortSignal): Promise<void> => {
  await browserApiClient.sessions.logout(signal)
}
