<template>
  <div class="tmux-controls">
    <div class="titlebar-menu desktop-action-menu">
      <button ref="desktopTrigger" data-action="toggle-tmux-menu" class="titlebar-button" :class="{ 'is-open': menuOpen }" type="button" :disabled="disabled" aria-haspopup="menu" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><Command :size="16" aria-hidden="true" /><span>tmux 操作</span><ChevronDown class="menu-chevron" :size="15" aria-hidden="true" /></button>
      <div v-if="menuOpen" class="floating-menu action-menu" role="menu" aria-label="tmux 操作" @keydown.esc.prevent="closeDesktopMenu">
        <label class="action-search"><span class="sr-only">搜索更多操作</span><input v-model="query" type="search" placeholder="搜索更多操作" /></label>
        <button v-for="action in filteredActions" :key="action.id" :data-action-id="action.id" type="button" :disabled="disabled" role="menuitem" :class="{ destructive: action.destructive }" :title="bindingTitle(action.id)" @click="choose(action)">{{ action.label }}<small>{{ bindingLabel(action.id) }}</small></button>
      </div>
    </div>
    <button ref="mobileTrigger" data-action="toggle-mobile-drawer" class="mobile-action-trigger" type="button" :disabled="disabled" :aria-expanded="drawerOpen" @click="drawerOpen = !drawerOpen">快捷操作</button>
    <aside v-if="drawerOpen" data-mobile-drawer class="mobile-action-drawer" aria-label="移动端 Tmux 操作" @keydown.esc.prevent.stop="closeDrawer" @pointerdown="beginDrawerSwipe" @pointerup="endDrawerSwipe">
      <header><strong>tmux 操作</strong><button class="icon-button drawer-collapse-button" type="button" @click="drawerOpen = false"><ChevronDown :size="17" aria-hidden="true" />收起</button></header>
      <div class="mobile-action-grid"><button v-for="action in tmuxActions" :key="action.id" type="button" :disabled="disabled" :title="bindingTitle(action.id)" @click="choose(action)">{{ action.label }}</button></div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { ChevronDown, Command } from '@lucide/vue'
import { computed, nextTick, ref } from 'vue'
import type { BindingSnapshotDto } from '../../api/types'
import type { TerminalActionId } from '../../api/types'
import { tmuxActions, type TmuxActionDefinition } from '../../terminal/actions'
const props = withDefaults(defineProps<{ bindings: BindingSnapshotDto; activePaneId: string | null; disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ action: [actionId: TerminalActionId, paneId: string | null]; 'request-close': [paneId: string | null, returnFocus: HTMLElement | null] }>()
const menuOpen = ref(false)
const drawerOpen = ref(false)
const mobileTrigger = ref<HTMLButtonElement | null>(null)
const desktopTrigger = ref<HTMLButtonElement | null>(null)
const query = ref('')
const drawerSwipeStartY = ref<number | null>(null)
const filteredActions = computed(() => tmuxActions.filter((action) => action.label.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
const binding = (id: TerminalActionId) => props.bindings.bindings.find((item) => item.action === id)
const bindingLabel = (id: TerminalActionId) => binding(id)?.key || '未绑定'
const bindingTitle = (id: TerminalActionId) => binding(id) ? `${binding(id)!.tooltip}；实际绑定：${bindingLabel(id)}` : '实际绑定：未绑定'
function choose(action: TmuxActionDefinition) {
  if (props.disabled) return
  const returnFocus = drawerOpen.value ? mobileTrigger.value : desktopTrigger.value
  menuOpen.value = false
  drawerOpen.value = false
  if (action.destructive) emit('request-close', props.activePaneId, returnFocus)
  else emit('action', action.id, props.activePaneId)
}
async function closeDrawer() { drawerOpen.value = false; await nextTick(); mobileTrigger.value?.focus() }
async function closeDesktopMenu() { menuOpen.value = false; await nextTick(); desktopTrigger.value?.focus() }
function beginDrawerSwipe(event: PointerEvent) { drawerSwipeStartY.value = event.clientY }
function endDrawerSwipe(event: PointerEvent) {
  const start = drawerSwipeStartY.value
  drawerSwipeStartY.value = null
  if (start !== null && event.clientY - start >= 60) void closeDrawer()
}
</script>
