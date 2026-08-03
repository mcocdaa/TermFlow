<template>
  <div class="page settings-page totp-activation-view">
    <header class="page-heading totp-guide-heading">
      <div class="totp-guide-heading-copy"><p class="eyebrow">Two Factor Authentication</p><h1>双重因素认证</h1></div>
      <RouterLink class="secondary-button" to="/settings">返回设置</RouterLink>
    </header>
    <ol v-if="!loading && status.available" class="totp-guide-steps" aria-label="激活步骤">
      <li
        v-for="(step, index) in guideSteps"
        :key="step"
        data-guide-step
        :data-state="guideStepState(index + 1)"
        :aria-current="guideStepState(index + 1) === 'current' ? 'step' : undefined"
      >
        <span class="totp-guide-step-marker" aria-hidden="true">{{ guideStepState(index + 1) === 'complete' ? '✓' : index + 1 }}</span>
        <span>{{ step }}</span>
      </li>
    </ol>
    <section class="settings-panel totp-guide-card" aria-live="polite">
      <p v-if="loading" role="status">正在读取安全状态…</p>
      <p v-else-if="!status.available" class="settings-warning" role="status">双重因素认证暂时不可用，请联系服务器管理员。</p>
      <template v-else>
        <header class="totp-wizard-card-heading">
          <h2 data-wizard-card-title>{{ wizardTitle }}</h2>
          <span data-wizard-progress>{{ wizardComplete ? '设置完成' : `第 ${currentStep} 步，共 3 步` }}</span>
        </header>
        <template v-if="setup">
          <div data-totp-bind-layout class="totp-bind-layout">
            <div class="totp-bind-qr">
              <ThemedQrCode :value="setup.provisioning_uri" alt="验证器设置二维码" />
              <button
                data-action="toggle-setup-key"
                class="setup-key-toggle"
                type="button"
                :aria-expanded="setupKeyExpanded"
                aria-controls="totp-setup-key"
                @click="setupKeyExpanded = !setupKeyExpanded"
              >无法扫描？使用设置密钥</button>
              <div v-if="setupKeyExpanded" id="totp-setup-key" class="setup-key-panel">
                <code data-setup-key>{{ setup.setup_key }}</code>
                <button data-action="copy-setup-key" class="compact-secondary-button" type="button" @click="copySetupKey">{{ setupKeyCopied ? '已复制' : '复制密钥' }}</button>
              </div>
            </div>
            <form data-action="confirm-totp-setup" class="inline-security-form totp-confirm-form" @submit.prevent="confirmSetup">
              <label for="activation-confirm-code">验证器验证码</label>
              <input id="activation-confirm-code" v-model="confirmCode" name="setup-confirm-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
              <p v-if="message" class="form-error" role="alert">{{ message }}</p>
              <button class="primary-button" type="submit" :disabled="busy">确认绑定</button>
            </form>
          </div>
        </template>
        <template v-else-if="!status.configured || reconfiguring">
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
          <div data-configured-authenticator-heading class="configured-authenticator-heading">
            <strong>验证器已绑定</strong>
            <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="reconfiguring = true">重新配置</button>
          </div>
          <div class="security-setting-row">
            <TotpProtectionLabel />
            <button ref="switchButton" class="toggle-switch" type="button" role="switch" :aria-checked="status.enabled" aria-label="启用双重认证登录" @click="dialogOpen = true"><span aria-hidden="true" /></button>
          </div>
        </template>
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
import { computed, onMounted, reactive, ref } from 'vue'
import ThemedQrCode from '../components/common/ThemedQrCode.vue'
import TotpProtectionLabel from '../components/settings/TotpProtectionLabel.vue'
import TotpProtectionDialog from '../components/settings/TotpProtectionDialog.vue'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const guideSteps = ['验证身份', '绑定验证器', '启用登录保护'] as const
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const loading = ref(true)
const busy = ref(false)
const reconfiguring = ref(false)
const setup = ref<TotpSetupResponse | null>(null)
const adminToken = ref('')
const currentCode = ref('')
const confirmCode = ref('')
const message = ref('')
const setupKeyExpanded = ref(false)
const setupKeyCopied = ref(false)
const dialogOpen = ref(false)
const switchButton = ref<HTMLButtonElement | null>(null)

const currentStep = computed<1 | 2 | 3>(() => {
  if (setup.value) return 2
  if (status.configured && !reconfiguring.value) return 3
  return 1
})
const wizardComplete = computed(() => status.enabled && !reconfiguring.value && setup.value === null)
const wizardTitle = computed(() => {
  if (setup.value) return '绑定验证器'
  if (status.configured && !reconfiguring.value) return status.enabled ? '双重认证已启用' : '启用登录保护'
  return status.configured ? '重新配置验证器' : '验证管理员身份'
})

function guideStepState(index: number) {
  if (wizardComplete.value || index < currentStep.value) return 'complete'
  return index === currentStep.value ? 'current' : 'upcoming'
}

function applyStatus(next: TotpStatusResponse) {
  Object.assign(status, next)
  reconfiguring.value = false
}

async function copySetupKey() {
  if (!setup.value) return
  await runtime.clipboard.writeText(setup.value.setup_key)
  setupKeyCopied.value = true
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
    setupKeyExpanded.value = false
    setupKeyCopied.value = false
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
    setupKeyExpanded.value = false
    setupKeyCopied.value = false
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
