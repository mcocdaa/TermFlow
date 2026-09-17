<template>
  <fieldset class="agent-panes" :disabled="disabled">
    <legend>
      {{ legend }}
      <ContextHelp label="窗格授权说明" text="窗格是终端里的一块分屏（% 编号）。Agent 只能读取勾选窗格的内容；写入仍会逐一弹审批。" />
    </legend>
    <div class="agent-panes__actions">
      <button type="button" :disabled="disabled || !panes.length" data-action="select-all-panes" @click="selectAll">全选</button>
      <button type="button" :disabled="disabled || !modelValue.length" data-action="clear-panes" @click="clear">清空</button>
      <span class="agent-panes__count">已选 {{ modelValue.length }}/{{ panes.length }}</span>
    </div>
    <div class="agent-panes__list">
      <label v-for="pane in panes" :key="pane.pane_id" class="agent-pane">
        <input type="checkbox" name="paneIds" :value="pane.pane_id" :checked="modelValue.includes(pane.pane_id)" @change="toggle(pane.pane_id, $event)" />
        <span class="agent-pane__text">
          <code class="agent-pane__id">{{ pane.pane_id }}</code>
          <span class="agent-pane__title">{{ pane.current_command || pane.title || pane.window_id }}</span>
        </span>
      </label>
      <p v-if="!panes.length" class="agent-panes__empty">暂无可用窗格，请连接终端后刷新。</p>
    </div>
  </fieldset>
</template>
<script setup lang="ts">
import type { PaneTopology } from '../../types'
import ContextHelp from '../common/ContextHelp.vue'
const props = withDefaults(defineProps<{ modelValue: string[]; panes: PaneTopology[]; legend?: string; disabled?: boolean }>(), { legend: '允许 Agent 访问的窗格', disabled: false })
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()
function toggle(paneId: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  emit('update:modelValue', checked ? [...props.modelValue, paneId] : props.modelValue.filter((id) => id !== paneId))
}
function selectAll() { emit('update:modelValue', props.panes.map((pane) => pane.pane_id)) }
function clear() { emit('update:modelValue', []) }
</script>
