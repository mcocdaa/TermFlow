<template>
  <div class="computer-table" role="table" :aria-label="t('computers.tableAria')">
    <div class="computer-table-head" role="row"><span role="columnheader">{{ t('computers.col.name') }}</span><span role="columnheader">{{ t('computers.col.terms') }}</span><span role="columnheader">{{ t('computers.col.lastSeen') }}</span><span role="columnheader">{{ t('computers.col.registeredAt') }}</span><span role="columnheader">{{ t('computers.col.actions') }}</span></div>
    <article v-for="computer in computers" :key="computer.installation_id" class="computer-table-row" role="row" :data-computer-id="computer.installation_id">
      <div role="cell" :data-label="t('computers.col.name')"><ComputerNameEditor :computer-id="computer.installation_id" :display-name="computer.display_name" @updated="computer.display_name = $event" /><span v-if="computer.hostname" class="muted">{{ computer.hostname }}</span></div>
      <div role="cell" :data-label="t('computers.col.terms')"><StatusPill :online="onlineTermCount(computer) > 0" :label="onlineTermCount(computer) > 0 ? t('status.onlineCount', { count: onlineTermCount(computer) }) : t('status.offlineZero')" /></div>
      <div role="cell" :data-label="t('computers.col.lastSeen')"><time v-if="computer.last_seen_at" :datetime="computer.last_seen_at">{{ format(computer.last_seen_at) }}</time><span v-else class="muted">{{ t('dashboard.neverOnline') }}</span></div>
      <div role="cell" :data-label="t('computers.col.registeredAt')"><time v-if="computer.registered_at" :datetime="computer.registered_at">{{ format(computer.registered_at) }}</time><span v-else class="muted">{{ t('computers.notRecorded') }}</span></div>
      <div role="cell" :data-label="t('computers.col.actions')" class="computer-table-actions"><button data-action="delete-computer" class="icon-button icon-only destructive" type="button" :disabled="onlineTermCount(computer) > 0" :aria-label="onlineTermCount(computer) > 0 ? t('computers.cannotDeleteOnlineAria', { name: computer.display_name }) : t('computers.deleteAria', { name: computer.display_name })" :title="onlineTermCount(computer) > 0 ? t('computers.cannotDeleteOnlineTitle') : t('computers.deleteAria', { name: computer.display_name })" @click="requestRemove(computer)"><Trash2 :size="17" aria-hidden="true" /></button></div>
    </article>
  </div>
</template>

<script setup lang="ts">
import { Trash2 } from '@lucide/vue'
import { useI18n } from '../../i18n'
import type { ComputerSummary } from '../../types'
import StatusPill from '../dashboard/StatusPill.vue'
import ComputerNameEditor from './ComputerNameEditor.vue'
import { formatBRecordedTime } from '../../utils/time'
const { t } = useI18n()
defineProps<{ computers: ComputerSummary[] }>()
const emit = defineEmits<{ remove: [computer: ComputerSummary] }>()
const format = formatBRecordedTime
const onlineTermCount = (computer: ComputerSummary) => computer.terms.filter((term) => term.online).length
function requestRemove(computer: ComputerSummary) {
  if (onlineTermCount(computer) > 0) return
  emit('remove', computer)
}
</script>
