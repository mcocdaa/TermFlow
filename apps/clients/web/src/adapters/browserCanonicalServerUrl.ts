type BrowserLocation = Pick<Location, 'origin'>

export function browserCanonicalServerUrl(location: BrowserLocation = globalThis.location): string {
  return location.origin
}
