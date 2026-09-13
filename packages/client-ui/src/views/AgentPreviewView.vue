<template>
  <section class="agent-chat-session agent-chat-session--page x-preview">
    <header class="agent-chat-header">
      <div class="agent-chat-header__left">
        <div>
          <p class="eyebrow">Agent Chat</p>
          <h1>部署检查</h1>
          <p class="agent-chat-header__binding">Binding 2e2b1253 · Term ca70e9a5</p>
        </div>
      </div>
      <div class="agent-chat-header__actions">
        <span class="agent-backend-status agent-backend-status--busy">处理中</span>
      </div>
    </header>
    <div class="agent-chat-body">
      <AgentPartsList :parts="parts" />
    </div>
    <section v-if="permission" class="agent-approval-prompt" data-preview-approvals>
      <AgentApprovalCard :permission="permission" @focus="() => {}" />
    </section>
    <div class="x-preview__composer">
      <span>输入消息…</span>
      <button type="button" class="icon-button icon-only" aria-label="发送消息"><Send :size="18" aria-hidden="true" /></button>
    </div>
  </section>
</template>

<script setup lang="ts">
//: Design preview for the rebuilt agent conversation window. Feeds the real
//: history reducer with sample AG-UI frames so the shipped components render
//: without any backend.
import AgentApprovalCard from '../components/agent/AgentApprovalCard.vue'
import AgentPartsList, { type AgentConversationPart } from '../components/agent/AgentPartsList.vue'
import { Send } from '@lucide/vue'
import {
  applyAguiEvent,
  createAgentHistoryState,
  seedUserMessages,
  type AgentHistoryState,
  type AguiEvent,
} from '@termflow/client-core'

const now = Date.now()
let state = createAgentHistoryState()
state = seedUserMessages(
  state,
  [
    {
      message_id: 'u1',
      conversation_id: 'c1',
      run_id: null,
      role: 'user',
      kind: 'text',
      assembly_revision: 1,
      is_final: false,
      body_digest: 'd',
      body: '请在当前允许的终端中执行 echo 1，并告诉我结果。',
      created_at: new Date(now).toISOString(),
    },
  ] as never,
  () => now,
)

const frames: AguiEvent[] = [
  { type: 'RUN_STARTED', threadId: 'c1', runId: 'r1' },
  { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/backend', value: { state: 'busy', epoch: 1 } }] },
  { type: 'CUSTOM', name: 'termflow.thinking_delta', value: { id: 'th1', delta: '用户想让我在终端执行 echo 1。我先列出 pane，确认可写后再发送命令并等待审批。' } },
  { type: 'CUSTOM', name: 'termflow.thinking_completed', value: { id: 'th1' } },
  { type: 'TEXT_MESSAGE_CHUNK', messageId: 'a1', delta: '我先看一下有哪些 pane，然后执行 echo 1。' },
  { type: 'TOOL_CALL_START', toolCallId: 't1', toolCallName: 'termflow_termflow_list_panes' },
  {
    type: 'TOOL_CALL_RESULT',
    messageId: 't1',
    toolCallId: 't1',
    content: '{"status":"success","truncated":false}',
  },
  { type: 'TEXT_MESSAGE_END', messageId: 'a1' },
  { type: 'TEXT_MESSAGE_CHUNK', messageId: 'a2', delta: '准备向 pane %0 写入 `echo 1`，需要你确认。' },
  {
    type: 'CUSTOM',
    name: 'termflow.permission_requested',
    value: { approval_request_id: 'a0000000-0000-4000-8000-000000000001' },
  },
] as AguiEvent[]

for (const frame of frames) state = applyAguiEvent(state, frame, () => now)

const history: AgentHistoryState = state
const parts: AgentConversationPart[] = [
  { type: 'step-start' },
  { type: 'reasoning', text: '用户想让我在终端执行 echo 1。我先列出 pane，确认可写后再发送命令并等待审批。' },
  { type: 'tool', tool: 'termflow_termflow_list_panes', status: 'completed', input: '{}', output: '{\n  "instance_id": "a38adddd-f204-4bd8-9587-0b57aa21524c",\n  "panes": [{"pane_id": "%0", "active": true, "dead": false}]\n}' },
  { type: 'reasoning', text: '只有一个 pane %0。发送 echo 1 并提交，然后读取输出。' },
  { type: 'tool', tool: 'termflow_termflow_pane_send_text', status: 'completed', input: '{"params": {"pane_id": "%0", "text": "echo 1", "submit": true, "intent": "Run echo 1"}}', output: '{\n  "ok": true,\n  "outcome": "confirmed",\n  "approval_id": "5282e3e1-..."\n}' },
  { type: 'tool', tool: 'termflow_termflow_pane_read', status: 'completed', input: '{"params": {"pane_id": "%0"}}', output: '"$ echo 1\\n1\\n$ "' },
  { type: 'text', text: '已执行。结果输出为 `1`。' },
  { type: 'step-finish' },
]
const permission = [...state.permissions.values()][0]
</script>

<style scoped>
.x-preview { max-width: 72rem; margin: 0 auto; padding: var(--space-4); }
.x-preview__composer { display: flex; align-items: center; gap: var(--space-2); margin-block-start: var(--space-3); padding: var(--space-2); border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: var(--color-elevated); color: var(--color-text-muted); }
.x-preview__composer > span { flex: 1; }
</style>
