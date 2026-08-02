<template>
  <section class="settings-panel" aria-labelledby="server-heading">
    <div class="settings-panel-heading">
      <div><p class="eyebrow">Server</p><h2 id="server-heading">中继服务器</h2></div>
    </div>
    <div data-server-label class="server-url-heading">
      <h3>服务网址</h3>
      <button
        ref="qrTrigger"
        data-action="show-server-qr"
        class="icon-button icon-only"
        type="button"
        aria-label="显示服务网址二维码"
        @click="qrOpen = true"
      >
        <QrCode :size="18" aria-hidden="true" />
      </button>
    </div>
    <div class="server-address-row">
      <code data-server-issuer>{{ issuer }}</code>
      <button data-action="copy-server-url" class="secondary-button" type="button" @click="copyIssuer">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <QrCodeDialog
      :open="qrOpen"
      title="服务网址二维码"
      :value="qrPayload"
      description="二维码仅包含公开服务网址和协议版本。"
      :return-focus="qrTrigger"
      @close="qrOpen = false"
    />
  </section>
</template>

<script setup lang="ts">
import { QrCode } from '@lucide/vue'
import { computed, ref } from 'vue'
import QrCodeDialog from '../common/QrCodeDialog.vue'
import { useClientRuntime } from '../../runtime'

const props = defineProps<{ issuer: string }>()
const runtime = useClientRuntime()
const copied = ref(false)
const qrOpen = ref(false)
const qrTrigger = ref<HTMLButtonElement | null>(null)
const qrPayload = computed(() => JSON.stringify({
  protocol: 'termflow-connect-v1',
  issuer: props.issuer,
}))

async function copyIssuer() {
  await runtime.clipboard.writeText(props.issuer)
  copied.value = true
}
</script>
