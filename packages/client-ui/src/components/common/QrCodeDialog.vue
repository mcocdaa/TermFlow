<template>
  <div v-if="open" class="dialog-backdrop qr-dialog-backdrop" @click.self="requestClose">
    <section
      class="dialog-panel qr-dialog-panel"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="titleId"
      :aria-describedby="descriptionId"
      @keydown="onKeydown"
    >
      <header>
        <h2 :id="titleId">{{ title }}</h2>
        <button ref="closeButton" data-action="close-qr" class="icon-button icon-only" type="button" aria-label="关闭二维码" @click="requestClose">
          <X :size="18" aria-hidden="true" />
        </button>
      </header>
      <ThemedQrCode :value="value" :alt="title" />
      <p :id="descriptionId">{{ description }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { X } from '@lucide/vue'
import { nextTick, ref, watch } from 'vue'
import ThemedQrCode from './ThemedQrCode.vue'

const props = defineProps<{
  open: boolean
  title: string
  value: string
  description: string
  returnFocus?: HTMLElement | null
}>()
const emit = defineEmits<{ close: [] }>()
const closeButton = ref<HTMLButtonElement | null>(null)
const titleId = `qr-title-${crypto.randomUUID()}`
const descriptionId = `qr-description-${crypto.randomUUID()}`

function restoreFocus() {
  void nextTick(() => {
    if (props.returnFocus?.isConnected) props.returnFocus.focus()
  })
}

function requestClose() {
  emit('close')
  restoreFocus()
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  requestClose()
}

watch(() => props.open, (open) => {
  if (open) void nextTick(() => closeButton.value?.focus())
  else restoreFocus()
}, { immediate: true })
</script>
