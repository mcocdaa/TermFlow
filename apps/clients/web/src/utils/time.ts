const bTimestampFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZoneName: 'short',
})

export function formatBRecordedTime(value: string) {
  return bTimestampFormatter.format(new Date(value))
}
