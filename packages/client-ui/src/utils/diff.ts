export interface DiffLine {
  type: 'add' | 'del' | 'hunk' | 'header' | 'normal'
  content: string
}

export function isUnifiedDiff(text: string): boolean {
  if (!text || typeof text !== 'string') return false
  if (/\b(?:diff --git|--- [ab]\/|\+\+\+ [ab]\/|@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)/.test(text)) {
    return true
  }
  const lines = text.split('\n')
  let hasPlus = false
  let hasMinus = false
  for (const line of lines) {
    if (line.startsWith('+') && !line.startsWith('+++')) hasPlus = true
    if (line.startsWith('-') && !line.startsWith('---')) hasMinus = true
  }
  return hasPlus && hasMinus && lines.length >= 2
}

export function parseUnifiedDiff(text: string): DiffLine[] {
  const lines = text.split('\n')
  return lines.map((line) => {
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff --git')) {
      return { type: 'header', content: line }
    }
    if (line.startsWith('@@') && line.includes('@@', 2)) {
      return { type: 'hunk', content: line }
    }
    if (line.startsWith('+')) {
      return { type: 'add', content: line }
    }
    if (line.startsWith('-')) {
      return { type: 'del', content: line }
    }
    return { type: 'normal', content: line }
  })
}
