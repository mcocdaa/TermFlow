import type { ClientRuntime } from './runtime'
import type { PaneSnapshot, TerminalAction, TerminalBinding } from '@termflow/client-contracts'

export type DashboardSnapshot = Awaited<ReturnType<ClientRuntime['api']['dashboard']['get']>>
export type ComputerList = Awaited<ReturnType<ClientRuntime['api']['computers']['list']>>
export type ComputerSummary = ComputerList['computers'][number]
export type TermSummary = ComputerSummary['terms'][number]
export type PaneTopology = PaneSnapshot
export type TerminalActionId = TerminalAction
export interface BindingSnapshot {
  prefix: string
  prefix2?: string | null
  bindings: TerminalBinding[]
}
