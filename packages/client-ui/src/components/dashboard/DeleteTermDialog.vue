<template>
  <div class="dialog-backdrop" @click.self="cancel">
    <section
      ref="panel"
      class="dialog-panel delete-term-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="delete-term-title"
      aria-describedby="delete-term-description"
      @keydown="trapFocus"
    >
      <h2 id="delete-term-title">永久删除离线 Term？</h2>
      <div id="delete-term-description">
        <p>将永久删除远端注册和控制凭据：<strong>{{ term.name }}</strong>（{{ term.instance_id }}）。</p>
        <p>这不会删除本地 tmux Session；它仍可能在所属 Computer 上运行。</p>
        <p>如需恢复远程访问，请在所属 Computer 上运行：</p>
        <code class="delete-term-activation">termflow activate {{ term.instance_id }}</code>
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button
          ref="cancelButton"
          data-action="cancel-delete-term"
          class="text-button"
          type="button"
          :disabled="pending"
          @click="cancel"
        >取消</button>
        <button
          data-action="confirm-delete-term"
          class="danger-button"
          type="button"
          :disabled="pending"
          @click="$emit('confirm', term.instance_id)"
        >{{ pending ? '正在删除…' : '永久删除远程 Term' }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { TermSummary } from '../../types'

const props = defineProps<{ term: TermSummary; pending: boolean; error: string }>()
const emit = defineEmits<{ confirm: [instanceId: string]; cancel: [] }>()
const panel = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

function cancel() {
  if (!props.pending) emit('cancel')
}

function trapFocus(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (!props.pending) {
      event.preventDefault()
      cancel()
    }
    return
  }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>('button:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}

onMounted(async () => {
  restoreFocus = document.activeElement as HTMLElement | null
  await nextTick()
  cancelButton.value?.focus()
})
onBeforeUnmount(() => { if (restoreFocus?.isConnected) restoreFocus.focus() })
</script>
