export const themeIds = [
  'graphite-signal',
  'cloud-cobalt',
  'midnight-indigo',
  'catppuccin-mocha',
  'catppuccin-latte',
  'tokyo-night',
  'nord',
  'dracula',
  'solarized-dark',
  'solarized-light',
] as const
export type ThemeId = (typeof themeIds)[number]
