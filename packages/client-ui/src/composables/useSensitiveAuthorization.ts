import { ApiError } from '@termflow/client-core'
import { ref } from 'vue'
import { useClientRuntime, type ClientRuntime } from '../runtime'

function createAuthorization(runtime: ClientRuntime) {
  const open = ref(false)
  let pending: Promise<boolean> | null = null
  let settle: ((result: boolean) => void) | null = null
  let nativeController: AbortController | null = null
  function finish(result: boolean) { open.value = false; settle?.(result); settle = null; if (!result) nativeController?.abort() }
  function authorize(signal?: AbortSignal): Promise<boolean> {
    if (signal?.aborted) return Promise.resolve(false)
    if (pending) return pending
    open.value = true
    const onAbort = () => finish(false)
    signal?.addEventListener('abort', onAbort, { once: true })
    const attempt = new Promise<boolean>((resolve) => { settle = resolve })
    if (runtime.sensitiveAuthorization.mode === 'native-oauth') {
      const ownController = new AbortController(); nativeController = ownController
      const ownSettle = settle
      void Promise.resolve().then(() => {
        if (ownController.signal.aborted) return 'cancelled'
        return runtime.sensitiveAuthorization.authorizeNative?.(ownController.signal)
      }).then((result) => ownSettle?.(result === 'authenticated' && !ownController.signal.aborted), () => ownSettle?.(false))
    }
    pending = attempt.catch(() => false).finally(() => {
      signal?.removeEventListener('abort', onAbort)
      open.value = false
      pending = null
      settle = null
      nativeController = null
    })
    return pending
  }
  async function run<T>(operation: () => Promise<T>, code: 'approval_reauthentication_required' | 'sensitive_action_reauthentication_required', signal?: AbortSignal): Promise<T> {
    try { return await operation() } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 428 || error.code !== code) throw error
      if (!await authorize(signal) || signal?.aborted) throw new Error('sensitive_authorization_cancelled')
      return operation()
    }
  }
  return { open, authorize, finish, run }
}
const authorizations = new WeakMap<ClientRuntime, ReturnType<typeof createAuthorization>>()
export function useSensitiveAuthorization() {
  const runtime = useClientRuntime()
  let state = authorizations.get(runtime)
  if (!state) { state = createAuthorization(runtime); authorizations.set(runtime, state) }
  return state
}
