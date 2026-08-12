import type { OAuthMetadataResponse } from '@termflow/client-contracts'
import { logNativeEvent } from './diagnostics'
import { canonicalIssuer, serverConfig } from './serverConfig'

export type NativeServerPreparation = {
  issuer: string
  metadata: OAuthMetadataResponse
}

export async function prepareNativeServer(
  input: string,
  loadMetadata: () => Promise<OAuthMetadataResponse>,
): Promise<NativeServerPreparation> {
  const issuer = canonicalIssuer(input)
  void logNativeEvent({ event: 'metadata_started', issuer })
  try {
    await serverConfig.replace(issuer)
    const metadata = await loadMetadata()
    if (metadata.issuer !== issuer) throw new Error('issuer_mismatch')
    void logNativeEvent({ event: 'metadata_succeeded', issuer })
    return { issuer, metadata }
  } catch (error) {
    const errorCode = error instanceof Error && error.message === 'issuer_mismatch'
      ? 'issuer_mismatch'
      : 'metadata_failed'
    void logNativeEvent({ event: 'metadata_failed', issuer, level: 'error', errorCode })
    throw error
  }
}
