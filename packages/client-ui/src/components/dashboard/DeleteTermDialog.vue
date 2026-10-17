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
      <h2 id="delete-term-title">{{ t('dashboard.deleteDialog.title') }}</h2>
      <div id="delete-term-description">
        <p>{{ t('dashboard.deleteDialog.desc1') }}<strong>{{ term.name }}</strong>{{ locale === 'en' ? ` (${term.instance_id}).` : `（${term.instance_id}）。` }}</p>
        <p>{{ t('dashboard.deleteDialog.desc2') }}</p>
        <p>{{ t('dashboard.deleteDialog.desc3') }}</p>
        <code class="delete-term-activation">termflow activate {{ term.instance_id }}</code>
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="cleanupJobId" role="status">{{ t('dashboard.deleteDialog.cleanupPending', { jobId: cleanupJobId.slice(0, 64) }) }}</p>
      <div class="dialog-actions">
        <button
          ref="cancelButton"
          data-action="cancel-delete-term"
          class="text-button"
          type="button"
          :disabled="pending"
          @click="cancel"
        >{{ t('common.cancel') }}</button>
        <button
          data-action="confirm-delete-term"
          class="danger-button"
          type="button"
          :disabled="pending || Boolean(cleanupJobId)"
          @click="$emit('confirm', term.instance_id)"
        >{{ pending ? t('dashboard.deleteDialog.deleting') : t('dashboard.deleteDialog.confirm') }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from '../../i18n'
import type { TermSummary } from '../../types'
const { t, locale } = useI18n()

const props = defineProps<{ term: TermSummary; pending: boolean; error: string; cleanupJobId?: string }>()
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
