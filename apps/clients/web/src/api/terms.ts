import { browserApiClient } from './http'
import type { TermSummaryDto, TopologyResponseDto } from './types'

export const getTermTopology = (id: string, signal?: AbortSignal): Promise<TopologyResponseDto> => browserApiClient.terms.topology(id, signal)
export const renameTerm = (id: string, name: string, signal?: AbortSignal): Promise<TermSummaryDto> => browserApiClient.terms.rename(id, name, signal)
