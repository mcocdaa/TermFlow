import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, safeMessageFor, type ApiErrorKind } from '@termflow/client-core'
import { locale, t } from '../i18n'
import { useClientRuntime } from '../runtime'
import type { DashboardSnapshot } from '../types'

const POLL_INTERVAL_MS = 15_000

export function useDashboard() {
  const runtime = useClientRuntime()
  const snapshot = ref<DashboardSnapshot | null>(null)
  const loading = ref(true)
  const errorState = ref<{ type: 'api'; kind: ApiErrorKind } | { type: 'key'; key: string } | null>(null)
  const message = computed(() => {
    void locale.value
    if (errorState.value === null) return ''
    return errorState.value.type === 'api' ? safeMessageFor(errorState.value.kind) : t(errorState.value.key)
  })
  let controller: AbortController | null = null
  let timer: unknown | null = null
  let unsubscribeVisibility: (() => void) | null = null
  let disposed = false

  function clearTimer() {
    if (timer !== null) runtime.clock.clearTimeout(timer)
    timer = null
  }

  async function load() {
    if (disposed || runtime.visibility.isHidden()) return
    controller?.abort()
    controller = new AbortController()
    try {
      snapshot.value = await runtime.api.dashboard.get(controller.signal)
      errorState.value = null
    } catch (error) {
      if (!(error instanceof ApiError) || error.kind !== 'aborted') {
        errorState.value = error instanceof ApiError ? { type: 'api', kind: error.kind } : { type: 'key', key: 'dashboard.loadError' }
      }
    } finally {
      loading.value = false
      clearTimer()
      if (!disposed && !runtime.visibility.isHidden()) timer = runtime.clock.setTimeout(() => { void load() }, POLL_INTERVAL_MS)
    }
  }

  function onVisibilityChange() {
    if (runtime.visibility.isHidden()) {
      controller?.abort()
      clearTimer()
    } else void load()
  }

  onMounted(() => {
    unsubscribeVisibility = runtime.visibility.subscribe(onVisibilityChange)
    void load()
  })
  onBeforeUnmount(() => {
    disposed = true
    controller?.abort()
    clearTimer()
    unsubscribeVisibility?.()
    unsubscribeVisibility = null
  })

  return { snapshot, loading, message, refresh: load }
}
