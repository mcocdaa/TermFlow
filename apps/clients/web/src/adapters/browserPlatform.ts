type PlatformSource = Pick<Navigator, 'platform'>

export function browserPlatform(source: PlatformSource = globalThis.navigator): string {
  return source.platform
}
