<template>
  <div class="page agent-chat-view">
    <template v-if="!agentBrokerEnabled">
      <header class="page-heading">
        <div><p class="eyebrow">Agent</p><h1>Agent 控制台</h1></div>
      </header>
      <section class="agent-placeholder" data-agent-disabled>
        <h2>Agent Broker 未启用</h2>
        <p>服务器未开启 Agent Broker 能力，无法使用 Agent 会话。</p>
      </section>
    </template>

    <!--
      Keyed by the route param: /agent/a → /agent/b reuses this view
      (App.vue keys RouterView with 'shared-client-route' for non-terminal
      routes), so without the key the conversation-scoped state inside
      (stream, history, approval panel, detail header) would keep showing
      the previous conversation while new messages went to the new one.
      The key remounts the whole conversation body for the new scope.
    -->
    <AgentChatSession
      v-else
      :key="conversationId"
      :conversation-id="conversationId"
      :enabled="agentBrokerEnabled"
    />
  </div>
</template>

<script setup lang="ts">
//: Route wrapper for the conversation-scoped Agent chat (M6b spec
//: §4.7/§4.8). Owns the capability gate (a disabled broker renders the
//: placeholder and the stream never starts) and delegates the whole
//: conversation body to AgentChatSession, keyed by the conversation id so
//: route-param changes re-scope every piece of conversation state.
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AgentChatSession from '../components/agent/AgentChatSession.vue'
import { useAgentBroker } from '../composables/useAgentBroker'

const route = useRoute()
const conversationId = computed(() => String(route.params.conversationId))

const { agentBrokerEnabled } = useAgentBroker()
</script>
