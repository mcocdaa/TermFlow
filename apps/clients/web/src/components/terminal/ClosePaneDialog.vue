<template>
  <div class="dialog-backdrop" @click.self="$emit('cancel')">
    <section ref="panel" class="dialog-panel close-pane-dialog" role="alertdialog" aria-modal="true" aria-labelledby="close-pane-title" aria-describedby="close-pane-description" @keydown="trapFocus">
      <h2 id="close-pane-title">关闭 Pane？</h2>
      <p id="close-pane-description">将关闭 <strong>{{ paneName }}</strong>（{{ paneId }}）。Pane 中未保存的工作可能丢失。</p>
      <div class="dialog-actions"><button ref="cancelButton" class="text-button" type="button" @click="$emit('cancel')">取消</button><button data-action="confirm-close-pane" class="danger-button" type="button" @click="$emit('confirm', { paneId, confirmed: true })">确认关闭 Pane</button></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
const props = defineProps<{ paneId: string; paneName: string }>()
defineEmits<{ confirm: [payload: { paneId: string; confirmed: true }]; cancel: [] }>()
const panel = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null
function trapFocus(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); cancelButton.value?.click(); return }
  if (event.key !== 'Tab' || !panel.value) return
  const focusable = [...panel.value.querySelectorAll<HTMLElement>('button:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { restoreFocus = document.activeElement as HTMLElement | null; await nextTick(); cancelButton.value?.focus() })
onBeforeUnmount(() => restoreFocus?.focus())
</script>
