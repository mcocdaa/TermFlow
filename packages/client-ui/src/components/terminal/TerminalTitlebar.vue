<template>
  <header class="terminal-titlebar">
    <RouterLink data-action="back-dashboard" class="terminal-back" to="/" :aria-label="t('terminal.backDashboard')"><ArrowLeft :size="19" aria-hidden="true" /></RouterLink>
    <div class="terminal-identity">
      <span class="terminal-light" :data-status="status" aria-hidden="true" />
      <div class="terminal-metadata">
        <div data-terminal-identifiers class="terminal-identifiers">
          <form v-if="editing" class="terminal-name-form" @submit.prevent="save">
            <label class="sr-only" for="terminal-name-input">{{ t('terminal.nameLabel') }}</label>
            <input id="terminal-name-input" ref="nameInput" v-model="draft" data-term-name-input maxlength="128" required @keydown.escape.prevent="cancel" />
            <button data-action="save-term-name" class="icon-button icon-only compact" type="submit" :aria-label="t('terminal.saveNameAria')" :title="t('common.save')"><Check :size="16" aria-hidden="true" /></button>
            <button class="icon-button icon-only compact" type="button" :aria-label="t('terminal.cancelNameAria')" :title="t('common.cancel')" @click="cancel"><X :size="16" aria-hidden="true" /></button>
            <span v-if="validationError" class="form-error" role="alert">{{ validationError }}</span>
          </form>
          <button v-else data-term-name data-action="edit-term-name" class="terminal-name-trigger" type="button" :aria-label="t('terminal.editNameAria', { title })" :title="t('terminal.editNameTitle')" @click="startEditing"><strong>{{ title }}</strong></button>
          <small data-computer-name :title="resolvedComputerName">{{ resolvedComputerName }}</small>
        </div>
      </div>
      <span data-connection-status class="terminal-status">{{ statusLabel }}</span>
    </div>
    <div class="terminal-titlebar-actions">
      <DisplayMenu :model-value="displayMode" :open="displayMenuOpen ?? false" @update:model-value="$emit('update:displayMode', $event)" @update:open="$emit('update:displayMenuOpen', $event)" />
      <slot />
      <button
        data-action="toggle-locale"
        class="titlebar-button locale-toggle-button"
        type="button"
        :aria-label="t('app.switchLangAria')"
        :title="t('app.switchLangAria')"
        @click="toggleLocale"
      >
        <Globe :size="15" aria-hidden="true" />
        <span class="locale-toggle-badge">{{ t('app.switchLangBadge') }}</span>
      </button>
      <button
        data-action="toggle-viewport-lock"
        class="titlebar-button viewport-lock-button"
        type="button"
        :aria-label="viewportLocked ? t('terminal.unlockViewport') : t('terminal.lockViewport')"
        :title="viewportLocked ? t('terminal.unlockViewport') : t('terminal.lockViewport')"
        :aria-pressed="viewportLocked"
        @click="emit('update:viewportLocked', !viewportLocked)"
      >
        <Lock v-if="viewportLocked" :size="16" aria-hidden="true" />
        <Unlock v-else :size="16" aria-hidden="true" />
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ArrowLeft, Check, Globe, Lock, Unlock, X } from '@lucide/vue'
import { computed, nextTick, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { TerminalConnectionStatus } from '@termflow/client-core'
import { useI18n } from '../../i18n'
import type { DisplayMode } from '../../terminal/viewport'
import DisplayMenu from './DisplayMenu.vue'
const { t, toggleLocale } = useI18n()
const props = withDefaults(defineProps<{ title: string; computerName?: string | undefined; status?: TerminalConnectionStatus; displayMode: DisplayMode; displayMenuOpen?: boolean; viewportLocked?: boolean }>(), { status: 'connecting', displayMenuOpen: false, viewportLocked: false })
const emit = defineEmits<{ 'update:displayMode': [mode: DisplayMode]; 'update:displayMenuOpen': [open: boolean]; 'update:viewportLocked': [locked: boolean]; rename: [name: string] }>()
const editing = ref(false)
const draft = ref('')
const validationError = ref('')
const nameInput = ref<HTMLInputElement | null>(null)
const resolvedComputerName = computed(() => props.computerName ?? t('terminal.unreportedComputer'))
const statusLabel = computed(() => t(`terminal.status.${props.status}`))
function startEditing() { draft.value = props.title; validationError.value = ''; editing.value = true; void nextTick(() => nameInput.value?.focus()) }
function cancel() { editing.value = false; validationError.value = '' }
function save() {
  const name = draft.value.trim()
  if (!name || name.length > 128 || /[\u0000-\u001f\u007f]/.test(name)) { validationError.value = t('terminal.nameInvalid'); return }
  emit('rename', name)
  cancel()
}
</script>
