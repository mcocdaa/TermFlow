import { locale } from '../i18n'

const zhTimestampFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

const enTimestampFormatter = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

export function formatBRecordedTime(value: string) {
  const formatter = locale.value === 'en' ? enTimestampFormatter : zhTimestampFormatter
  return formatter.format(new Date(value))
}
