<template>
  <div class="dialog-backdrop" @click.self="$emit('cancel')">
    <section ref="panel" class="dialog-panel close-pane-dialog" role="alertdialog" aria-modal="true" aria-labelledby="close-pane-title" aria-describedby="close-pane-description" @keydown="trapFocus">
      <h2 id="close-pane-title">{{ t('terminal.closePane.title') }}</h2>
      <p id="close-pane-description">{{ t('terminal.closePane.desc', { name: paneName, id: paneId }) }}</p>
      <div class="dialog-actions"><button ref="cancelButton" class="text-button" type="button" @click="$emit('cancel')">{{ t('common.cancel') }}</button><button data-action="confirm-close-pane" class="danger-button" type="button" @click="$emit('confirm', { paneId, confirmed: true })">{{ t('terminal.closePane.confirm') }}</button></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from '../../i18n'
const { t } = useI18n()
const props = defineProps<{ paneId: string; paneName: string; returnFocus?: HTMLElement | null }>()
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
onMounted(async () => { restoreFocus = props.returnFocus ?? document.activeElement as HTMLElement | null; await nextTick(); cancelButton.value?.focus() })
onBeforeUnmount(() => { if (restoreFocus?.isConnected) restoreFocus.focus() })
</script>
