export const themeIds = ['graphite-signal', 'cloud-cobalt', 'midnight-indigo'] as const
export type ThemeId = (typeof themeIds)[number]
