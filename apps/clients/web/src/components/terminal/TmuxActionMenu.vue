<template>
  <div class="tmux-controls">
    <div class="titlebar-menu desktop-action-menu">
      <button ref="desktopTrigger" data-action="toggle-tmux-menu" class="titlebar-button" :class="{ 'is-open': open }" type="button" :disabled="disabled" aria-label="tmux 操作" aria-haspopup="menu" :aria-expanded="open" @click="$emit('update:open', !open)"><Command :size="16" aria-hidden="true" /><span class="control-label">tmux 操作</span><ChevronDown class="menu-chevron" :size="15" aria-hidden="true" /></button>
      <div v-if="open" class="floating-menu action-menu" role="menu" aria-label="tmux 操作" @keydown.esc.prevent="closeDesktopMenu">
        <label class="action-search"><span class="sr-only">搜索更多操作</span><input v-model="query" type="search" placeholder="搜索更多操作" /></label>
        <button v-for="action in filteredActions" :key="action.id" :data-action-id="action.id" type="button" :disabled="disabled" role="menuitem" :class="{ destructive: action.destructive }" :aria-describedby="bindingTooltipId(action.id)" @click="choose(action)">
          <span class="action-label">{{ action.label }}</span>
          <span :id="bindingTooltipId(action.id)" class="action-binding-tooltip" role="tooltip"><template v-if="bindingKey(action.id)">快捷键 <code>{{ readableBinding(action.id) }}</code></template><template v-else>未绑定</template></span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, Command } from '@lucide/vue'
import { computed, nextTick, ref, useId } from 'vue'
import type { BindingSnapshotDto } from '../../api/types'
import type { TerminalActionId } from '../../api/types'
import { tmuxActions, type TmuxActionDefinition } from '../../terminal/actions'
const props = withDefaults(defineProps<{ bindings: BindingSnapshotDto; activePaneId: string | null; disabled?: boolean; open?: boolean }>(), { disabled: false, open: false })
const emit = defineEmits<{ action: [actionId: TerminalActionId, paneId: string | null]; 'request-close': [paneId: string | null, returnFocus: HTMLElement | null]; 'update:open': [open: boolean] }>()
const desktopTrigger = ref<HTMLButtonElement | null>(null)
const query = ref('')
const tooltipScope = useId()
const filteredActions = computed(() => tmuxActions.filter((action) => action.label.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
const binding = (id: TerminalActionId) => props.bindings.bindings.find((item) => item.action === id)
const bindingKey = (id: TerminalActionId) => binding(id)?.key ?? null
const bindingTooltipId = (id: TerminalActionId) => `tmux-binding-${tooltipScope}-${id}`
function readableKeyToken(token: string) {
  const segments = token.split('-')
  const key = segments.pop() ?? token
  const modifiers = segments.map((modifier) => ({ C: 'Ctrl', M: 'Alt', S: 'Shift' })[modifier] ?? modifier)
  return [...modifiers, key].join(' + ')
}
function readableBinding(id: TerminalActionId) { return (bindingKey(id) ?? '').split(/\s+/).filter(Boolean).map(readableKeyToken).join('  ') }
function choose(action: TmuxActionDefinition) {
  if (props.disabled) return
  emit('update:open', false)
  if (action.destructive) emit('request-close', props.activePaneId, desktopTrigger.value)
  else emit('action', action.id, props.activePaneId)
}
async function closeDesktopMenu() { emit('update:open', false); await nextTick(); desktopTrigger.value?.focus() }
</script>
