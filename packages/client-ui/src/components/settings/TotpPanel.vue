<template>
  <section class="settings-panel" aria-labelledby="totp-heading">
    <div class="settings-panel-heading">
      <div>
        <p class="eyebrow">Two Factor Authentication</p>
        <div class="settings-heading-label">
          <h2 id="totp-heading">双重因素认证</h2>
          <ContextHelp
            v-if="!loading && status.available && !status.configured"
            data-action="explain-totp"
            label="说明双重因素认证"
            text="绑定验证器 App 后，可为新的登录和客户端授权增加一次性验证码。"
          />
        </div>
      </div>
      <div v-if="!loading && status.configured" data-authenticator-actions class="settings-panel-heading-actions">
        <span class="status-chip">验证器已绑定</span>
        <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="activate">重新配置</button>
      </div>
      <span v-else-if="!loading" class="status-chip" data-status="disabled">未激活</span>
    </div>
    <p v-if="loading" role="status">正在读取安全状态…</p>
    <template v-else-if="!status.available">
      <p class="settings-warning" role="status">双重因素认证暂时不可用，请联系服务器管理员。</p>
    </template>
    <template v-else-if="!status.configured">
      <button data-action="activate-totp" class="primary-button settings-action-button" type="button" @click="activate">激活双重因素认证</button>
    </template>
    <template v-else>
      <div class="security-setting-row">
        <TotpProtectionLabel />
        <button
          ref="switchButton"
          class="toggle-switch"
          type="button"
          role="switch"
          :aria-checked="status.enabled"
          aria-label="启用双重认证登录"
          @click="openProtectionDialog"
        ><span aria-hidden="true" /></button>
      </div>
    </template>
    <TotpProtectionDialog
      :open="dialogOpen"
      :target-enabled="!status.enabled"
      :return-focus="switchButton"
      @close="dialogOpen = false"
      @confirmed="applyStatus"
    />
  </section>
</template>

<script setup lang="ts">
import type { TotpStatusResponse } from '@termflow/client-contracts'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import ContextHelp from '../common/ContextHelp.vue'
import { useClientRuntime } from '../../runtime'
import TotpProtectionLabel from './TotpProtectionLabel.vue'
import TotpProtectionDialog from './TotpProtectionDialog.vue'

const emit = defineEmits<{ changed: [TotpStatusResponse] }>()
const runtime = useClientRuntime()
const router = useRouter()
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const loading = ref(true)
const dialogOpen = ref(false)
const switchButton = ref<HTMLButtonElement | null>(null)

function applyStatus(next: TotpStatusResponse) {
  Object.assign(status, next)
  emit('changed', next)
}

function activate() {
  void router.push('/settings/two-factor-auth')
}

function openProtectionDialog() {
  dialogOpen.value = true
}

onMounted(async () => {
  try {
    applyStatus(await runtime.api.security.totpStatus())
  } catch {
    Object.assign(status, { configured: false, enabled: false, available: false })
  } finally {
    loading.value = false
  }
})
</script>
