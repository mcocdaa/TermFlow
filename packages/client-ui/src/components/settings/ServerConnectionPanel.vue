<template>
  <section class="settings-panel" aria-labelledby="server-heading">
    <div class="settings-panel-heading">
      <div><p class="eyebrow">Server</p><h2 id="server-heading">B 连接地址</h2></div>
      <span class="status-chip">只读</span>
    </div>
    <p class="settings-copy">此地址由服务器部署配置决定，Web 管理页不能修改。</p>
    <div class="server-address-row">
      <code data-server-issuer>{{ issuer }}</code>
      <button class="secondary-button" type="button" @click="copyIssuer">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <div class="connection-qr">
      <img v-if="qrDataUrl" :src="qrDataUrl" alt="TermFlow Server 连接二维码" />
      <p>二维码只包含公开 issuer 和协议版本，不包含管理员 Token、TOTP 或客户端凭据。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import QRCode from 'qrcode'
import { onMounted, ref, watch } from 'vue'
import { useClientRuntime } from '../../runtime'

const props = defineProps<{ issuer: string }>()
const runtime = useClientRuntime()
const qrDataUrl = ref('')
const copied = ref(false)

async function renderQr() {
  qrDataUrl.value = await QRCode.toDataURL(JSON.stringify({ protocol: 'termflow-connect-v1', issuer: props.issuer }), {
    errorCorrectionLevel: 'M', margin: 1, width: 208,
  })
}

async function copyIssuer() {
  await runtime.clipboard.writeText(props.issuer)
  copied.value = true
}

onMounted(renderQr)
watch(() => props.issuer, renderQr)
</script>
