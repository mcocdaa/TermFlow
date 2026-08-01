export interface SessionStatusDto {
  authenticated: boolean
  expires_at?: string
}

export interface DashboardMetricsDto {
  online_terms: number
  active_panes: number
  interactions_24h: number
  computers: number
}

export interface TermSummaryDto {
  term_id: string
  computer_id: string
  name: string
  online: boolean
  window_count: number
  pane_count: number
  pane_current_command: string | null
  last_seen_at: string | null
}

export interface ComputerSummaryDto {
  computer_id: string
  display_name: string
  hostname: string
  platform: string
  client_version: string
  online: boolean
  online_term_count: number
  registered_at: string
  last_seen_at: string | null
  terms: TermSummaryDto[]
}

export interface DashboardDto {
  metrics: DashboardMetricsDto
  computers: ComputerSummaryDto[]
}

export interface ComputerListDto { computers: ComputerSummaryDto[] }
export interface ComputerDetailDto extends ComputerSummaryDto {}

export interface EnrollmentCodeDto {
  code: string
  expires_at: string
}

export interface PaneTopologyDto {
  pane_id: string
  title: string
  current_command: string
  active: boolean
  left: number
  top: number
  width: number
  height: number
}

export interface WindowTopologyDto {
  window_id: string
  name: string
  active: boolean
  panes: PaneTopologyDto[]
}

export interface TermDetailDto extends TermSummaryDto { windows: WindowTopologyDto[] }

export interface BindingSnapshotDto {
  prefix: string
  actions: Record<string, string | null>
}
