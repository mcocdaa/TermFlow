import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getDashboard } from '../api/dashboard'
import { ApiError } from '../api/http'
import type { DashboardDto } from '../api/types'

const POLL_INTERVAL_MS = 15_000
const pageIsHidden = () => document.visibilityState === 'hidden'

export function useDashboard() {
  const snapshot = ref<DashboardDto | null>(null)
  const loading = ref(true)
  const message = ref('')
  let controller: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | null = null

  function clearTimer() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  async function load() {
    if (pageIsHidden()) return
    controller?.abort()
    controller = new AbortController()
    try {
      snapshot.value = await getDashboard(controller.signal)
      message.value = ''
    } catch (error) {
      if (!(error instanceof ApiError) || error.kind !== 'aborted') message.value = error instanceof ApiError ? error.message : '无法加载控制中心。'
    } finally {
      loading.value = false
      clearTimer()
      if (!pageIsHidden()) timer = setTimeout(load, POLL_INTERVAL_MS)
    }
  }

  function onVisibilityChange() {
    if (pageIsHidden()) {
      controller?.abort()
      clearTimer()
    } else void load()
  }

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibilityChange)
    void load()
  })
  onBeforeUnmount(() => {
    controller?.abort()
    clearTimer()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { snapshot, loading, message, refresh: load }
}
