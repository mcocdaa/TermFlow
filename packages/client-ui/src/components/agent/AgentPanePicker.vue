<template>
  <fieldset class="agent-panes" :disabled="disabled">
    <legend>
      {{ legend ?? t('agent.panes.defaultLegend') }}
      <ContextHelp :label="t('agent.panes.helpLabel')" :text="t('agent.panes.helpText')" />
    </legend>
    <div class="agent-panes__actions">
      <button type="button" :disabled="disabled || !panes.length" data-action="select-all-panes" @click="selectAll">{{ t('agent.panes.selectAll') }}</button>
      <button type="button" :disabled="disabled || !modelValue.length" data-action="clear-panes" @click="clear">{{ t('agent.panes.clear') }}</button>
      <span class="agent-panes__count">{{ t('agent.panes.selectedCount', { selected: modelValue.length, total: panes.length }) }}</span>
    </div>
    <div class="agent-panes__list">
      <label v-for="pane in panes" :key="pane.pane_id" class="agent-pane">
        <input type="checkbox" name="paneIds" :value="pane.pane_id" :checked="modelValue.includes(pane.pane_id)" @change="toggle(pane.pane_id, $event)" />
        <span class="agent-pane__text">
          <code class="agent-pane__id">{{ pane.pane_id }}</code>
          <span class="agent-pane__title">{{ pane.current_command || pane.title || pane.window_id }}</span>
        </span>
      </label>
      <p v-if="!panes.length" class="agent-panes__empty">{{ t('agent.panes.empty') }}</p>
    </div>
  </fieldset>
</template>
<script setup lang="ts">
import { useI18n } from '../../i18n'
import type { PaneTopology } from '../../types'
import ContextHelp from '../common/ContextHelp.vue'
const { t } = useI18n()
const props = withDefaults(defineProps<{ modelValue: string[]; panes: PaneTopology[]; legend?: string; disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()
function toggle(paneId: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  emit('update:modelValue', checked ? [...props.modelValue, paneId] : props.modelValue.filter((id) => id !== paneId))
}
function selectAll() { emit('update:modelValue', props.panes.map((pane) => pane.pane_id)) }
function clear() { emit('update:modelValue', []) }
</script>
