<template>
  <div class="computer-table" role="table" aria-label="已注册 Computers">
    <div class="computer-table-head" role="row"><span role="columnheader">名称与主机</span><span role="columnheader">操作系统</span><span role="columnheader">Terms</span><span role="columnheader">注册 / 最近在线</span></div>
    <article v-for="computer in computers" :key="computer.installation_id" class="computer-table-row" role="row" :data-computer-id="computer.installation_id">
      <div role="cell"><ComputerNameEditor :computer-id="computer.installation_id" :display-name="computer.display_name" @updated="computer.display_name = $event" /><span v-if="computer.hostname" class="muted">{{ computer.hostname }}</span></div>
      <div role="cell"><strong v-if="computer.platform">{{ computer.platform }}</strong><span v-else class="muted">未识别</span><span v-if="computer.client_version" class="muted">客户端版本 {{ computer.client_version }}</span></div>
      <div role="cell"><strong>{{ computer.terms.filter((term) => term.online).length }} 个在线 Term</strong><StatusPill :online="computer.online" /></div>
      <div role="cell"><span v-if="computer.registered_at">注册：<time :datetime="computer.registered_at">{{ format(computer.registered_at) }}</time></span><span v-else>注册：未报告</span><span v-if="computer.last_seen_at">最近：<time :datetime="computer.last_seen_at">{{ format(computer.last_seen_at) }}</time></span></div>
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
</script>
