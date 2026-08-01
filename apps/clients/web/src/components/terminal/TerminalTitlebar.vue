<template>
  <header class="terminal-titlebar">
    <RouterLink data-action="back-dashboard" class="terminal-back" to="/" aria-label="返回控制中心"><ArrowLeft :size="19" aria-hidden="true" /></RouterLink>
    <div class="terminal-identity">
      <span class="terminal-light" :data-status="status" aria-hidden="true" />
      <div class="terminal-metadata">
        <form v-if="editing" class="terminal-name-form" @submit.prevent="save">
          <label class="sr-only" for="terminal-name-input">Term 名称</label>
          <input id="terminal-name-input" ref="nameInput" v-model="draft" data-term-name-input maxlength="128" required @keydown.escape.prevent="cancel" />
          <button data-action="save-term-name" class="icon-button icon-only compact" type="submit" aria-label="保存 Term 名称" title="保存"><Check :size="16" aria-hidden="true" /></button>
          <button class="icon-button icon-only compact" type="button" aria-label="取消修改 Term 名称" title="取消" @click="cancel"><X :size="16" aria-hidden="true" /></button>
          <span v-if="validationError" class="form-error" role="alert">{{ validationError }}</span>
        </form>
        <div v-else class="terminal-name-row">
          <strong data-term-name>{{ title }}</strong>
          <button data-action="edit-term-name" class="icon-button icon-only compact" type="button" aria-label="编辑 Term 名称" title="编辑 Term 名称" @click="startEditing"><Pencil :size="15" aria-hidden="true" /></button>
        </div>
        <small data-computer-name>{{ computerName }}</small>
      </div>
      <span data-connection-status class="terminal-status">{{ statusLabel }}</span>
    </div>
    <div class="terminal-titlebar-actions"><DisplayMenu :model-value="displayMode" @update:model-value="$emit('update:displayMode', $event)" /><slot /></div>
  </header>
</template>

<script setup lang="ts">
import { ArrowLeft, Check, Pencil, X } from '@lucide/vue'
import { computed, nextTick, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { DisplayMode } from '../../terminal/viewport'
import type { TerminalConnectionStatus } from '../../terminal/socket'
import DisplayMenu from './DisplayMenu.vue'
const props = withDefaults(defineProps<{ title: string; computerName?: string; status?: TerminalConnectionStatus; displayMode: DisplayMode }>(), { computerName: 'Computer 未报告', status: 'connecting' })
const emit = defineEmits<{ 'update:displayMode': [mode: DisplayMode]; rename: [name: string] }>()
const editing = ref(false)
const draft = ref('')
const validationError = ref('')
const nameInput = ref<HTMLInputElement | null>(null)
const statusLabel = computed(() => ({ connecting: '正在连接', connected: '已连接', reconnecting: '正在恢复连接', closed: '连接已关闭' })[props.status])
function startEditing() { draft.value = props.title; validationError.value = ''; editing.value = true; void nextTick(() => nameInput.value?.focus()) }
function cancel() { editing.value = false; validationError.value = '' }
function save() {
  const name = draft.value.trim()
  if (!name || name.length > 128 || /[\u0000-\u001f\u007f]/.test(name)) { validationError.value = '名称需为 1–128 个可见字符。'; return }
  emit('rename', name)
  cancel()
}
</script>
