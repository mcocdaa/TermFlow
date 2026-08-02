<template>
  <section class="settings-panel" aria-labelledby="totp-heading">
    <div class="settings-panel-heading">
      <div><p class="eyebrow">Security</p><h2 id="totp-heading">验证器双重验证</h2></div>
      <span class="status-chip" :data-status="status.enabled ? 'enabled' : 'disabled'">{{ status.enabled ? '已启用' : '未启用' }}</span>
    </div>
    <p v-if="!status.available" class="settings-warning" role="status">服务器尚未配置独立的 TOTP 加密主密钥，此功能不可用。</p>
    <template v-else-if="setup">
      <div class="totp-setup-material">
        <img :src="setupQr" alt="验证器设置二维码" />
        <div><p>扫码后输入第一个有效验证码。密钥只在本次设置中显示。</p><code data-setup-key>{{ setup.setup_key }}</code></div>
      </div>
      <form class="inline-security-form" @submit.prevent="confirmSetup">
        <label for="setup-code">6 位验证码</label>
        <input id="setup-code" v-model="confirmCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
        <button class="primary-button" type="submit" :disabled="busy">确认启用</button>
      </form>
    </template>
    <form v-else class="security-form" @submit.prevent="status.enabled ? disable() : beginSetup()">
      <p>{{ status.enabled ? '关闭或重新配置需要管理员 Token 和当前的新验证码。' : '启用时需要重新输入管理员 Token。' }}</p>
      <label for="totp-admin-token">管理员 Token</label>
      <input id="totp-admin-token" v-model="adminToken" type="password" autocomplete="off" required />
      <template v-if="status.enabled">
        <label for="current-totp">当前验证码</label>
        <input id="current-totp" v-model="currentCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
      </template>
      <p v-if="message" class="form-error" role="alert">{{ message }}</p>
      <div v-if="status.enabled" class="dialog-actions">
        <button class="secondary-button" type="button" :disabled="busy" @click="beginSetup">重新配置</button>
        <button class="danger-button" type="submit" :disabled="busy">关闭双重验证</button>
      </div>
      <button v-else class="primary-button" type="submit" :disabled="busy">开始设置</button>
    </form>
    <p class="settings-footnote">TermFlow 不提供恢复码、邮件或远程重置；丢失验证器只能由服务器管理员在容器内重置。</p>
  </section>
</template>

<script setup lang="ts">
import type { TotpSetupResponse, TotpStatusResponse } from '@termflow/client-contracts'
import QRCode from 'qrcode'
import { onMounted, reactive, ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../../runtime'

const emit = defineEmits<{ changed: [TotpStatusResponse] }>()
const runtime = useClientRuntime()
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const setup = ref<TotpSetupResponse | null>(null)
const setupQr = ref('')
const adminToken = ref('')
const currentCode = ref('')
const confirmCode = ref('')
const busy = ref(false)
const message = ref('')

async function loadStatus() {
  const next = await runtime.api.security.totpStatus()
  Object.assign(status, next)
  emit('changed', next)
}
async function beginSetup() {
  busy.value = true; message.value = ''
  try {
    setup.value = await runtime.api.security.createTotpSetup({ adminToken: adminToken.value, ...(currentCode.value ? { totpCode: currentCode.value } : {}) })
    setupQr.value = await QRCode.toDataURL(setup.value.provisioning_uri, { errorCorrectionLevel: 'M', margin: 1, width: 208 })
  } catch (error) { message.value = error instanceof ApiError ? error.message : '无法开始设置，请重试。' }
  finally { adminToken.value = ''; currentCode.value = ''; busy.value = false }
}
async function confirmSetup() {
  if (setup.value === null) return
  busy.value = true; message.value = ''
  try {
    const next = await runtime.api.security.confirmTotpSetup(setup.value.setup_id, confirmCode.value)
    Object.assign(status, next); setup.value = null; setupQr.value = ''; confirmCode.value = ''; emit('changed', next)
  } catch (error) { message.value = error instanceof ApiError ? error.message : '验证码无效或设置已过期。' }
  finally { confirmCode.value = ''; busy.value = false }
}
async function disable() {
  busy.value = true; message.value = ''
  try {
    await runtime.api.security.disableTotp({ adminToken: adminToken.value, totpCode: currentCode.value })
    const next = { configured: true, enabled: false, available: true }
    Object.assign(status, next); emit('changed', next)
  } catch (error) { message.value = error instanceof ApiError ? error.message : '无法关闭双重验证。' }
  finally { adminToken.value = ''; currentCode.value = ''; busy.value = false }
}
onMounted(() => { void loadStatus() })
</script>
