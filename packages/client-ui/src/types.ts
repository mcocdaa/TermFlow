import type { ClientRuntime } from './runtime'

export type DashboardSnapshot = Awaited<ReturnType<ClientRuntime['api']['dashboard']['get']>>
export type ComputerList = Awaited<ReturnType<ClientRuntime['api']['computers']['list']>>
export type ComputerSummary = ComputerList['computers'][number]
export type TermSummary = ComputerSummary['terms'][number]
