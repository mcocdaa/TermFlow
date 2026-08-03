<template>
  <div class="page settings-page totp-activation-view">
    <header class="page-heading totp-guide-heading">
      <div class="totp-guide-heading-copy"><p class="eyebrow">Two Factor Authentication</p><h1>双重因素认证</h1><p>使用你的验证器 App 完成绑定，再决定是否对新登录启用保护。</p></div>
      <RouterLink class="secondary-button" to="/settings">返回设置</RouterLink>
    </header>
    <ol class="totp-guide-steps" aria-label="激活步骤">
      <li v-for="step in guideSteps" :key="step" data-guide-step>{{ step }}</li>
    </ol>
    <section class="settings-panel totp-guide-card" aria-live="polite">
      <p v-if="loading" role="status">正在读取安全状态…</p>
      <p v-else-if="!status.available" class="settings-warning" role="status">双重因素认证暂时不可用，请联系服务器管理员。</p>
      <template v-else-if="setup">
        <div class="totp-setup-material">
          <ThemedQrCode :value="setup.provisioning_uri" alt="验证器设置二维码" />
          <div class="totp-setup-copy"><h2>扫描二维码</h2><p>在验证器 App 中扫码，或手工输入下面的设置密钥。</p><code data-setup-key>{{ setup.setup_key }}</code></div>
        </div>
        <form data-action="confirm-totp-setup" class="inline-security-form" @submit.prevent="confirmSetup">
          <label for="activation-confirm-code">验证器生成的第一个 6 位验证码</label>
          <input id="activation-confirm-code" v-model="confirmCode" name="setup-confirm-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
          <p v-if="message" class="form-error" role="alert">{{ message }}</p>
          <button class="primary-button" type="submit" :disabled="busy">确认绑定</button>
        </form>
      </template>
      <template v-else-if="!status.configured || reconfiguring">
        <div class="totp-guide-intro"><h2>{{ status.configured ? '重新配置验证器' : '验证管理员身份' }}</h2><p>管理员 Token 只用于本次验证，不会保存在客户端。</p></div>
        <form data-action="begin-totp-setup" class="security-form" @submit.prevent="beginSetup">
          <label for="activation-admin-token">管理员 Token</label>
          <input id="activation-admin-token" v-model="adminToken" name="setup-admin-token" type="password" autocomplete="off" required />
          <template v-if="status.configured">
            <label for="activation-current-code">当前验证器验证码</label>
            <input id="activation-current-code" v-model="currentCode" name="setup-current-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
          </template>
          <p v-if="message" class="form-error" role="alert">{{ message }}</p>
          <button class="primary-button" type="submit" :disabled="busy">继续</button>
        </form>
      </template>
      <template v-else>
        <div class="totp-guide-intro"><h2>验证器已绑定</h2><p>绑定完成。登录保护目前{{ status.enabled ? '已开启' : '未开启' }}。</p></div>
        <div class="security-setting-row">
          <TotpProtectionLabel />
          <button ref="switchButton" class="toggle-switch" type="button" role="switch" :aria-checked="status.enabled" aria-label="启用双重认证登录" @click="dialogOpen = true"><span aria-hidden="true" /></button>
        </div>
        <button class="secondary-button settings-action-button" type="button" @click="reconfiguring = true">重新配置验证器</button>
      </template>
    </section>
    <TotpProtectionDialog
      :open="dialogOpen"
      :target-enabled="!status.enabled"
      :return-focus="switchButton"
      @close="dialogOpen = false"
      @confirmed="applyStatus"
    />
  </div>
</template>

<script setup lang="ts">
import type { TotpSetupResponse, TotpStatusResponse } from '@termflow/client-contracts'
import { onMounted, reactive, ref } from 'vue'
import ThemedQrCode from '../components/common/ThemedQrCode.vue'
import TotpProtectionLabel from '../components/settings/TotpProtectionLabel.vue'
import TotpProtectionDialog from '../components/settings/TotpProtectionDialog.vue'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const guideSteps = ['验证身份', '绑定验证器', '确认验证码', '保存绑定', '选择登录保护'] as const
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const loading = ref(true)
const busy = ref(false)
const reconfiguring = ref(false)
const setup = ref<TotpSetupResponse | null>(null)
const adminToken = ref('')
const currentCode = ref('')
const confirmCode = ref('')
const message = ref('')
const dialogOpen = ref(false)
const switchButton = ref<HTMLButtonElement | null>(null)

function applyStatus(next: TotpStatusResponse) {
  Object.assign(status, next)
  reconfiguring.value = false
}

async function beginSetup() {
  if (busy.value) return
  busy.value = true
  message.value = ''
  try {
    setup.value = await runtime.api.security.createTotpSetup({
      adminToken: adminToken.value,
      ...(status.configured ? { totpCode: currentCode.value } : {}),
    })
  } catch {
    message.value = '验证失败，请检查凭据后重试。'
  } finally {
    adminToken.value = ''
    currentCode.value = ''
    busy.value = false
  }
}

async function confirmSetup() {
  if (busy.value || setup.value === null) return
  busy.value = true
  message.value = ''
  try {
    const next = await runtime.api.security.confirmTotpSetup(setup.value.setup_id, confirmCode.value)
    applyStatus(next)
    setup.value = null
  } catch {
    message.value = '验证码无效或设置已过期，请重试。'
  } finally {
    confirmCode.value = ''
    busy.value = false
  }
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
