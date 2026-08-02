import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../runtime'
import type { DashboardSnapshot } from '../types'

const POLL_INTERVAL_MS = 15_000

export function useDashboard() {
  const runtime = useClientRuntime()
  const snapshot = ref<DashboardSnapshot | null>(null)
  const loading = ref(true)
  const message = ref('')
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
      message.value = ''
    } catch (error) {
      if (!(error instanceof ApiError) || error.kind !== 'aborted') message.value = error instanceof ApiError ? error.message : '无法加载控制中心。'
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
