<template>
  <div class="tmux-controls">
    <div class="titlebar-menu desktop-action-menu" @mouseenter="preview = true" @mouseleave="preview = false">
      <button data-action="toggle-tmux-menu" class="titlebar-button" type="button" aria-haspopup="menu" :aria-expanded="menuOpen" @click="pinned = !pinned">Tmux 操作 <span aria-hidden="true">▾</span></button>
      <div v-if="menuOpen" class="floating-menu action-menu" role="menu" aria-label="Tmux 操作">
        <label class="action-search"><span class="sr-only">搜索更多操作</span><input v-model="query" type="search" placeholder="搜索更多操作" /></label>
        <button v-for="action in filteredActions" :key="action.id" :data-action-id="action.id" type="button" role="menuitem" :class="{ destructive: action.destructive }" :title="bindingTitle(action.id)" @click="choose(action)">{{ action.label }}<small>{{ bindingLabel(action.id) }}</small></button>
      </div>
    </div>
    <button data-action="toggle-mobile-drawer" class="mobile-action-trigger" type="button" :aria-expanded="drawerOpen" @click="drawerOpen = !drawerOpen">快捷操作</button>
    <aside v-if="drawerOpen" data-mobile-drawer class="mobile-action-drawer" aria-label="移动端 Tmux 操作">
      <header><strong>Tmux 操作</strong><button class="icon-button" type="button" @click="drawerOpen = false">收起</button></header>
      <div class="mobile-action-grid"><button v-for="action in tmuxActions" :key="action.id" type="button" :title="bindingTitle(action.id)" @click="choose(action)">{{ action.label }}</button></div>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { BindingSnapshotDto } from '../../api/types'
import { tmuxActions, type TmuxActionDefinition } from '../../terminal/actions'
const props = defineProps<{ bindings: BindingSnapshotDto; activePaneId: string | null }>()
const emit = defineEmits<{ action: [actionId: string, paneId: string | null]; 'request-close': [paneId: string | null] }>()
const pinned = ref(false)
const preview = ref(false)
const drawerOpen = ref(false)
const query = ref('')
const menuOpen = computed(() => pinned.value || preview.value)
const filteredActions = computed(() => tmuxActions.filter((action) => action.label.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
const bindingLabel = (id: string) => props.bindings.actions[id] || '未绑定'
const bindingTitle = (id: string) => `实际绑定：${bindingLabel(id)}`
function choose(action: TmuxActionDefinition) {
  pinned.value = false
  drawerOpen.value = false
  if (action.destructive) emit('request-close', props.activePaneId)
  else emit('action', action.id, props.activePaneId)
}
</script>
