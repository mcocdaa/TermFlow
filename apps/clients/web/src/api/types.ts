import type {
  BrowserSessionResponse,
  ComputerListResponse,
  ComputerSummary,
  DashboardMetrics,
  DashboardResponse,
  EnrollmentCreateResponse,
  PaneSnapshot,
  TerminalAction,
  TerminalBinding,
  TermSummary,
  TopologyResponse,
  TopologySnapshot,
  WindowSnapshot,
} from '@termflow/client-contracts'

export type SessionStatusDto = BrowserSessionResponse
export type DashboardMetricsDto = DashboardMetrics
export type TermSummaryDto = TermSummary
export type ComputerSummaryDto = ComputerSummary
export type DashboardDto = DashboardResponse
export type ComputerListDto = ComputerListResponse
export type ComputerDetailDto = ComputerSummary
export type EnrollmentCodeDto = EnrollmentCreateResponse
export type PaneTopologyDto = PaneSnapshot
export type WindowTopologyDto = WindowSnapshot
export type TopologyDto = TopologySnapshot
export type TopologyResponseDto = TopologyResponse
export type TerminalActionId = TerminalAction
export type TerminalBindingDto = TerminalBinding

export interface BindingSnapshotDto {
  prefix: string
  prefix2?: string | null
  bindings: TerminalBindingDto[]
}
