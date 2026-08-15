//: Agent Broker capability gate (M6b spec §4.7). One unauthenticated
//: capabilities fetch on mount: `agent_broker_enabled` decides whether the
//: Agent navigation/views render, `delegated_write_grants_enabled` is
//: read-only (B pins it False). Fail-closed: a fetch failure keeps the gate
//: disabled so the UI never depends on Agent API 404s.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useClientRuntime } from '../runtime'

export function useAgentBroker() {
  const runtime = useClientRuntime()
  const agentBrokerEnabled = ref(false)
  const delegatedWriteGrantsEnabled = ref(false)
  const loading = ref(true)
  const failed = ref(false)
  let controller: AbortController | null = null
  let disposed = false

  async function load() {
    loading.value = true
    failed.value = false
    try {
      const capabilities = await runtime.api.agents.capabilities(controller?.signal)
      agentBrokerEnabled.value = capabilities.agent_broker_enabled
      delegatedWriteGrantsEnabled.value = capabilities.delegated_write_grants_enabled
    } catch {
      // Fail closed: the Agent UI stays hidden until the flag is known.
      agentBrokerEnabled.value = false
      delegatedWriteGrantsEnabled.value = false
      failed.value = true
    } finally {
      if (!disposed) loading.value = false
    }
  }

  onMounted(() => {
    controller = new AbortController()
    void load()
  })
  onBeforeUnmount(() => {
    disposed = true
    controller?.abort()
    controller = null
  })

  return { agentBrokerEnabled, delegatedWriteGrantsEnabled, loading, failed, reload: load }
}
