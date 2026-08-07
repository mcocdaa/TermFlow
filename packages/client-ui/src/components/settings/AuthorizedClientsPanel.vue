<template>
  <section class="settings-panel" aria-labelledby="clients-heading">
    <div class="settings-panel-heading"><div><p class="eyebrow">Access</p><h2 id="clients-heading">已授权客户端</h2></div><div class="page-heading-actions"><span class="status-chip">{{ clients.length }}</span><button data-action="authorize-new-client" class="text-button" type="button" title="输入设备码以批准另一台已发起连接的客户端。" @click="approvalOpen = true">授权新客户端</button></div></div>
    <p v-if="clients.length === 0" class="empty-state">尚未授权手机或桌面客户端。</p>
    <ul v-else class="authorized-client-list">
      <li v-for="client in clients" :key="client.client_id">
        <div><strong>{{ client.display_name }}</strong><span>{{ client.platform }} · {{ client.key_thumbprint.slice(0, 12) }}…</span><small>{{ client.scopes.join(' · ') }}</small></div>
        <button class="danger-button" type="button" @click="openRevoke(client)">撤销</button>
      </li>
    </ul>
    <div v-if="selected" class="dialog-backdrop" @click.self="cancel">
      <section ref="revokePanel" class="dialog-panel revoke-client-dialog" role="alertdialog" aria-modal="true" aria-labelledby="revoke-client-title" @keydown="trapFocus">
        <header><div><p class="eyebrow">Access</p><h2 id="revoke-client-title">撤销客户端</h2></div><button class="icon-button icon-only" type="button" aria-label="关闭" @click="cancel">×</button></header>
        <p>将注销 <strong>{{ selected.display_name }}</strong>，不会影响其他客户端或电脑端 A。</p>
        <form class="security-form" @submit.prevent="removeSelected">
          <label for="revoke-admin-token">管理员 Token</label><input id="revoke-admin-token" ref="revokeInput" v-model="adminToken" type="password" autocomplete="off" required />
          <label v-if="totpEnabled" for="revoke-totp">当前验证码</label><input v-if="totpEnabled" id="revoke-totp" v-model="totpCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required />
          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <div class="dialog-actions"><button type="button" class="secondary-button" @click="cancel">取消</button><button type="submit" class="danger-button">确认撤销</button></div>
        </form>
      </section>
    </div>
    <DeviceAuthorizationApprovalDialog v-if="approvalOpen" @approved="onApproved" @closed="approvalOpen = false" />
  </section>
</template>

<script setup lang="ts">
import type { NativeClientResponse } from '@termflow/client-contracts'
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useBottomToast } from '../../composables/useBottomToast'
import { useClientRuntime } from '../../runtime'
import DeviceAuthorizationApprovalDialog from './DeviceAuthorizationApprovalDialog.vue'

defineProps<{ totpEnabled: boolean }>()
const runtime = useClientRuntime()
const toast = useBottomToast()
const clients = ref<NativeClientResponse[]>([])
const selected = ref<NativeClientResponse | null>(null)
const adminToken = ref('')
const totpCode = ref('')
const error = ref('')
const approvalOpen = ref(false)
const revokePanel = ref<HTMLElement | null>(null)
const revokeInput = ref<HTMLInputElement | null>(null)
let restoreFocus: HTMLElement | null = null

async function load() { clients.value = (await runtime.api.clients.list()).clients.filter((client) => client.revoked_at === null) }
function clearSecrets() { adminToken.value = ''; totpCode.value = ''; error.value = '' }
function cancel() {
  selected.value = null
  clearSecrets()
  if (restoreFocus?.isConnected) restoreFocus.focus()
}
function trapFocus(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    cancel()
    return
  }
  if (event.key !== 'Tab' || !revokePanel.value) return
  const focusable = [...revokePanel.value.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
async function openRevoke(client: NativeClientResponse) {
  error.value = ''
  selected.value = client
  restoreFocus = document.activeElement as HTMLElement | null
  await nextTick()
  revokeInput.value?.focus()
}
async function removeSelected() {
  if (selected.value === null) return
  error.value = ''
  try {
    await runtime.api.clients.remove(selected.value.client_id, { adminToken: adminToken.value, ...(totpCode.value ? { totpCode: totpCode.value } : {}) })
    const removed = selected.value.client_id
    selected.value = null
    clearSecrets()
    clients.value = clients.value.filter((client) => client.client_id !== removed)
    toast.show({ text: '已撤销', tone: 'success' })
  } catch {
    error.value = '撤销失败，请检查凭据后重试。'
  }
}
async function onApproved() { approvalOpen.value = false; await load() }
onBeforeUnmount(() => { if (selected.value === null && restoreFocus?.isConnected) restoreFocus.focus() })
onMounted(() => { void load() })
</script>
