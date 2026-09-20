export type RiskLevel = 1 | 2 | 3 | 4

export interface RiskAssessment {
  level: RiskLevel
  label: string
  badgeClass: string
  isCritical: boolean
}

const CRITICAL_PATTERNS = [
  /(?:^|[;&|\s])rm\s+.*(-[a-zA-Z]*r[a-zA-Z]*f|--recursive|-[a-zA-Z]*f[a-zA-Z]*r)\b/i,
  /(?:^|[;&|\s])rm\s+.*(-r|-R)\b/i,
  /(?:^|[;&|\s])rmdir\b/i,
  /\bkill\s+-9\b/i,
  /\bkillall\s+-9\b/i,
  /\bdrop\s+(database|table|schema)\b/i,
  /\b(mkfs|fdisk|format\s+[a-zA-Z]:)\b/i,
  /\bdd\s+if=/i,
  /\bchmod\s+(-R\s+)?777\s+\//i,
  /:\(\)\{\s*:\|:&\s*\};:/,
  /\btruncate\s+/i,
  /\bgit\s+reset\s+--hard\b/i,
  /\bgit\s+clean\s+-[a-zA-Z]*f\b/i,
]

const HIGH_PATTERNS = [
  /\b(sudo|doas|su)\s+/i,
  /\b(chmod|chown)\b/i,
  /\bsystemctl\s+(stop|restart|disable|mask|poweroff|reboot)\b/i,
  /\bservice\s+\S+\s+(stop|restart)\b/i,
  /\b(shutdown|reboot|poweroff|init\s+0)\b/i,
  /\b(curl|wget)\s+.*\|\s*(bash|sh)\b/i,
  /\bdocker\s+(rm|rmi|system\s+prune|kill)\b/i,
  /\b(kill|pkill)\b/i,
  /\bgit\s+push\s+.*--force\b/i,
]

const LOW_PATTERNS = [
  /\b(cat|ls|grep|find|pwd|head|tail|less|more|which|whereis|whoami|uptime)\b/i,
  /\bgit\s+(status|diff|log|branch|show)\b/i,
  /\b(pane_read|list_panes|inspect|read)\b/i,
]

export function assessRisk(params: {
  operation?: string | null
  summary?: string | null
  toolName?: string | null
  evidence?: string | null
}): RiskAssessment {
  const parts = [
    params.operation ?? '',
    params.summary ?? '',
    params.toolName ?? '',
    params.evidence ?? '',
  ].filter(Boolean)
  const text = parts.join(' ')

  if (params.operation === 'rm' || params.toolName === 'rm') {
    if (CRITICAL_PATTERNS.some((p) => p.test(text)) || /(-r|--recursive|\*|\/)/i.test(text)) {
      return {
        level: 4,
        label: '极高危',
        badgeClass: 'agent-risk-badge--level-4',
        isCritical: true,
      }
    }
  }

  for (const pattern of CRITICAL_PATTERNS) {
    if (pattern.test(text)) {
      return {
        level: 4,
        label: '极高危',
        badgeClass: 'agent-risk-badge--level-4',
        isCritical: true,
      }
    }
  }

  for (const pattern of HIGH_PATTERNS) {
    if (pattern.test(text)) {
      return {
        level: 3,
        label: '高风险',
        badgeClass: 'agent-risk-badge--level-3',
        isCritical: false,
      }
    }
  }

  const isReadOnlyOp = params.operation === 'read' || params.operation === 'query' || params.operation === 'list'
  if (isReadOnlyOp || LOW_PATTERNS.some((p) => p.test(text))) {
    if (!/\b(rm|write|edit|delete|touch|mkdir|chmod|kill|git\s+commit|git\s+push)\b/i.test(text)) {
      return {
        level: 1,
        label: '低风险',
        badgeClass: 'agent-risk-badge--level-1',
        isCritical: false,
      }
    }
  }

  return {
    level: 2,
    label: '中风险',
    badgeClass: 'agent-risk-badge--level-2',
    isCritical: false,
  }
}
