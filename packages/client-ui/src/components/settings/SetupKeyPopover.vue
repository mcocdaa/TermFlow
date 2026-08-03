<template>
  <div ref="root" class="setup-key-disclosure" @keydown="onKeydown">
    <button
      ref="trigger"
      data-action="toggle-setup-key"
      class="setup-key-toggle"
      type="button"
      aria-haspopup="dialog"
      :aria-expanded="open"
      aria-controls="totp-setup-key-popover"
      @click="toggle"
    >无法扫描？使用设置密钥</button>
    <section
      v-if="open"
      id="totp-setup-key-popover"
      class="setup-key-popover"
      role="dialog"
      aria-labelledby="totp-setup-key-title"
    >
      <h3 id="totp-setup-key-title">设置密钥</h3>
      <code data-setup-key>{{ setupKey }}</code>
      <button
        data-action="copy-setup-key"
        class="compact-secondary-button"
        type="button"
        @click="$emit('copy')"
      >{{ copied ? '已复制' : '复制密钥' }}</button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

defineProps<{
  setupKey: string
  copied: boolean
}>()

defineEmits<{
  copy: []
}>()

const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)

function close() {
  if (!open.value) return
  open.value = false
  void nextTick(() => trigger.value?.focus())
}

function toggle() {
  if (open.value) {
    close()
    return
  }
  open.value = true
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !open.value) return
  event.preventDefault()
  event.stopPropagation()
  close()
}

function onDocumentPointerDown(event: Event) {
  if (!open.value || !(event.target instanceof Node)) return
  if (root.value?.contains(event.target)) return
  close()
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))
</script>
