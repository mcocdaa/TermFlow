const localTimestampFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

export function formatBRecordedTime(value: string) {
  return localTimestampFormatter.format(new Date(value))
}
