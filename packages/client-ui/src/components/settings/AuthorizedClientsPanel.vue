<template>
  <section class="settings-panel" aria-labelledby="clients-heading">
    <div class="settings-panel-heading"><div><p class="eyebrow">Access</p><h2 id="clients-heading">已授权客户端</h2></div><div class="page-heading-actions"><span class="status-chip">{{ clients.length }}</span><button data-action="authorize-new-client" class="text-button" type="button" title="输入设备码以批准另一台已发起连接的客户端。" @click="approvalOpen = true">授权新客户端</button></div></div>
    <p v-if="clients.length === 0" class="empty-state">尚未授权手机或桌面客户端。</p>
    <ul v-else class="authorized-client-list">
      <li v-for="client in clients" :key="client.client_id">
        <div><strong>{{ client.display_name }}</strong><span>{{ client.platform }} · {{ client.key_thumbprint.slice(0, 12) }}…</span><small>{{ client.scopes.join(' · ') }}</small></div>
        <button class="danger-button" type="button" @click="selected = client">撤销</button>
      </li>
    </ul>
    <form v-if="selected" class="security-form revoke-form" @submit.prevent="removeSelected">
      <h3>撤销 {{ selected.display_name }}？</h3>
      <p>只会注销这台客户端，不影响其他 C 或电脑端 A。</p>
      <label for="revoke-admin-token">管理员 Token</label><input id="revoke-admin-token" v-model="adminToken" type="password" autocomplete="off" required />
      <label v-if="totpEnabled" for="revoke-totp">当前验证码</label><input v-if="totpEnabled" id="revoke-totp" v-model="totpCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" required />
      <div class="dialog-actions"><button type="button" class="secondary-button" @click="cancel">取消</button><button type="submit" class="danger-button">确认撤销</button></div>
    </form>
    <DeviceAuthorizationApprovalDialog v-if="approvalOpen" @approved="onApproved" @closed="approvalOpen = false" />
  </section>
</template>

<script setup lang="ts">
import type { NativeClientResponse } from '@termflow/client-contracts'
import { onMounted, ref } from 'vue'
import { useClientRuntime } from '../../runtime'
import DeviceAuthorizationApprovalDialog from './DeviceAuthorizationApprovalDialog.vue'

defineProps<{ totpEnabled: boolean }>()
const runtime = useClientRuntime()
const clients = ref<NativeClientResponse[]>([])
const selected = ref<NativeClientResponse | null>(null)
const adminToken = ref('')
const totpCode = ref('')
const approvalOpen = ref(false)

async function load() { clients.value = (await runtime.api.clients.list()).clients.filter((client) => client.revoked_at === null) }
function cancel() { selected.value = null; adminToken.value = ''; totpCode.value = '' }
async function removeSelected() {
  if (selected.value === null) return
  await runtime.api.clients.remove(selected.value.client_id, { adminToken: adminToken.value, ...(totpCode.value ? { totpCode: totpCode.value } : {}) })
  const removed = selected.value.client_id
  cancel(); clients.value = clients.value.filter((client) => client.client_id !== removed)
}
async function onApproved() { approvalOpen.value = false; await load() }
onMounted(() => { void load() })
</script>
