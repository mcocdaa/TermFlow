<template>
  <div class="tmux-controls">
    <div class="titlebar-menu desktop-action-menu">
      <button ref="desktopTrigger" data-action="toggle-tmux-menu" class="titlebar-button" :class="{ 'is-open': open }" type="button" :disabled="disabled" aria-haspopup="menu" :aria-expanded="open" @click="$emit('update:open', !open)"><Command :size="16" aria-hidden="true" /><span>tmux 操作</span><ChevronDown class="menu-chevron" :size="15" aria-hidden="true" /></button>
      <div v-if="open" class="floating-menu action-menu" role="menu" aria-label="tmux 操作" @keydown.esc.prevent="closeDesktopMenu">
        <label class="action-search"><span class="sr-only">搜索更多操作</span><input v-model="query" type="search" placeholder="搜索更多操作" /></label>
        <button v-for="action in filteredActions" :key="action.id" :data-action-id="action.id" type="button" :disabled="disabled" role="menuitem" :class="{ destructive: action.destructive }" :aria-describedby="bindingTooltipId(action.id)" @click="choose(action)">
          <span class="action-label">{{ action.label }}</span>
          <span :id="bindingTooltipId(action.id)" class="action-binding-tooltip" role="tooltip"><template v-if="bindingKey(action.id)">快捷键 <code>{{ readableBinding(action.id) }}</code></template><template v-else>未绑定</template></span>
        </button>
      </div>
    </div>
    <button ref="mobileTrigger" data-action="toggle-mobile-drawer" class="mobile-action-trigger" type="button" :disabled="disabled" :aria-expanded="drawerOpen" @click="drawerOpen = !drawerOpen">快捷操作</button>
    <aside v-if="drawerOpen" data-mobile-drawer class="mobile-action-drawer" aria-label="移动端 Tmux 操作" @keydown.esc.prevent.stop="closeDrawer" @pointerdown="beginDrawerSwipe" @pointerup="endDrawerSwipe">
      <header><strong>tmux 操作</strong><button class="icon-button drawer-collapse-button" type="button" @click="drawerOpen = false"><ChevronDown :size="17" aria-hidden="true" />收起</button></header>
      <div class="mobile-action-grid"><button v-for="action in tmuxActions" :key="action.id" type="button" :disabled="disabled" @click="choose(action)">{{ action.label }}</button></div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, Command } from '@lucide/vue'
import { computed, nextTick, ref, useId } from 'vue'
import type { BindingSnapshot, TerminalActionId } from '../../types'
import { tmuxActions, type TmuxActionDefinition } from '../../terminal/actions'
const props = withDefaults(defineProps<{ bindings: BindingSnapshot; activePaneId: string | null; disabled?: boolean; open?: boolean }>(), { disabled: false, open: false })
const emit = defineEmits<{ action: [actionId: TerminalActionId, paneId: string | null]; 'request-close': [paneId: string | null, returnFocus: HTMLElement | null]; 'update:open': [open: boolean] }>()
const drawerOpen = ref(false)
const mobileTrigger = ref<HTMLButtonElement | null>(null)
const desktopTrigger = ref<HTMLButtonElement | null>(null)
const query = ref('')
const tooltipScope = useId()
const drawerSwipeStartY = ref<number | null>(null)
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
  const returnFocus = drawerOpen.value ? mobileTrigger.value : desktopTrigger.value
  emit('update:open', false)
  drawerOpen.value = false
  if (action.destructive) emit('request-close', props.activePaneId, returnFocus)
  else emit('action', action.id, props.activePaneId)
}
async function closeDrawer() { drawerOpen.value = false; await nextTick(); mobileTrigger.value?.focus() }
async function closeDesktopMenu() { emit('update:open', false); await nextTick(); desktopTrigger.value?.focus() }
function beginDrawerSwipe(event: PointerEvent) { drawerSwipeStartY.value = event.clientY }
function endDrawerSwipe(event: PointerEvent) {
  const start = drawerSwipeStartY.value
  drawerSwipeStartY.value = null
  if (start !== null && event.clientY - start >= 60) void closeDrawer()
}
</script>
