export interface SessionStatusDto {
  authenticated: boolean
  expires_at?: string
}

export interface DashboardMetricsDto {
  online_terms: number
  total_terms: number
  active_panes: number
  interactions_24h: number
  computers: number
}

export interface TermSummaryDto {
  instance_id: string
  name: string
  online: boolean
  window_count: number
  pane_count: number
  active_pane_count: number
  current_command: string | null
  last_seen_at: string | null
}

export interface ComputerSummaryDto {
  installation_id: string
  display_name: string
  hostname: string | null
  platform: string | null
  client_version: string | null
  online: boolean
  registered_at?: string | null
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
  token: string
  expires_at: string
}

export interface PaneTopologyDto {
  pane_id: string
  window_id: string
  index: number
  title: string
  current_command: string | null
  active: boolean
  dead: boolean
  left: number
  top: number
  width: number
  height: number
}

export interface WindowTopologyDto {
  window_id: string
  index: number
  name: string
  active: boolean
  panes: PaneTopologyDto[]
}

export interface TopologyDto { session_id: string; session_name: string; revision: number; windows: WindowTopologyDto[] }
export interface TopologyResponseDto { instance_id: string; topology: TopologyDto }

export type TerminalActionId = 'split_left_right' | 'split_top_bottom' | 'new_window' | 'select_left' | 'select_right' | 'select_up' | 'select_down' | 'toggle_zoom' | 'copy_mode' | 'close_pane'
export interface TerminalBindingDto { action: TerminalActionId; key: string | null; tooltip: string }

export interface BindingSnapshotDto {
  prefix: string
  prefix2?: string | null
  bindings: TerminalBindingDto[]
}
