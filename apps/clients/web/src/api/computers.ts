import { browserApiClient } from './http'
import type { ComputerDetailDto, ComputerListDto, EnrollmentCodeDto } from './types'

export const listComputers = (signal?: AbortSignal): Promise<ComputerListDto> => browserApiClient.computers.list(signal)
export const getComputer = (id: string, signal?: AbortSignal): Promise<ComputerDetailDto> => browserApiClient.computers.get(id, signal)
export const renameComputer = (id: string, displayName: string, signal?: AbortSignal): Promise<ComputerDetailDto> => browserApiClient.computers.rename(id, displayName, signal)
export const createEnrollmentCode = (displayName?: string, signal?: AbortSignal): Promise<EnrollmentCodeDto> => browserApiClient.computers.createEnrollment(displayName, signal)
