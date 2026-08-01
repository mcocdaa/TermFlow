<template>
  <div class="computer-table" role="table" aria-label="已注册 Computers">
    <div class="computer-table-head" role="row"><span role="columnheader">名称</span><span role="columnheader">终端</span><span role="columnheader">最近在线</span><span role="columnheader">注册时间</span></div>
    <article v-for="computer in computers" :key="computer.installation_id" class="computer-table-row" role="row" :data-computer-id="computer.installation_id">
      <div role="cell" data-label="名称"><ComputerNameEditor :computer-id="computer.installation_id" :display-name="computer.display_name" @updated="computer.display_name = $event" /><span v-if="computer.hostname" class="muted">{{ computer.hostname }}</span></div>
      <div role="cell" data-label="终端"><StatusPill :online="onlineTermCount(computer) > 0" :label="onlineTermCount(computer) > 0 ? `在线 (${onlineTermCount(computer)})` : '离线 (0)'" /></div>
      <div role="cell" data-label="最近在线"><time v-if="computer.last_seen_at" :datetime="computer.last_seen_at">{{ format(computer.last_seen_at) }}</time><span v-else class="muted">尚未在线</span></div>
      <div role="cell" data-label="注册时间"><time v-if="computer.registered_at" :datetime="computer.registered_at">{{ format(computer.registered_at) }}</time><span v-else class="muted">未记录</span></div>
    </article>
  </div>
</template>

<script setup lang="ts">
import type { ComputerSummaryDto } from '../../api/types'
import StatusPill from '../dashboard/StatusPill.vue'
import ComputerNameEditor from './ComputerNameEditor.vue'
import { formatBRecordedTime } from '../../utils/time'
defineProps<{ computers: ComputerSummaryDto[] }>()
const format = formatBRecordedTime
const onlineTermCount = (computer: ComputerSummaryDto) => computer.terms.filter((term) => term.online).length
</script>
