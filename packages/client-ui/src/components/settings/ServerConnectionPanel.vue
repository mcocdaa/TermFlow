<template>
  <section class="settings-panel" aria-labelledby="server-heading">
    <div class="settings-panel-heading">
      <div><p class="eyebrow">Server</p><h2 id="server-heading">{{ t('settings.server.heading') }}</h2></div>
    </div>
    <div data-server-field class="server-field">
      <div class="server-field-heading">
        <span id="server-url-label" data-server-label class="field-label">{{ t('settings.server.urlLabel') }}</span>
        <button
          ref="qrTrigger"
          data-action="show-server-qr"
          class="icon-button icon-only server-qr-trigger"
          type="button"
          :aria-label="t('settings.server.qrAria')"
          @click="qrOpen = true"
        >
          <QrCode :size="18" aria-hidden="true" />
        </button>
      </div>
      <div class="server-address-row" aria-labelledby="server-url-label">
        <code id="server-url-value" data-server-issuer>{{ issuer }}</code>
        <button data-action="copy-server-url" class="secondary-button" type="button" @click="copyIssuer">{{ copied ? t('common.copied') : t('common.copy') }}</button>
      </div>
    </div>
    <QrCodeDialog
      :open="qrOpen"
      :title="t('settings.server.qrTitle')"
      :value="qrPayload"
      :return-focus="qrTrigger"
      @close="qrOpen = false"
    />
  </section>
</template>

<script setup lang="ts">
import { QrCode } from '@lucide/vue'
import { computed, ref } from 'vue'
import QrCodeDialog from '../common/QrCodeDialog.vue'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'

const { t } = useI18n()
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
