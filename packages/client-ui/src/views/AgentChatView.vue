<template>
  <div class="page agent-chat-view">
    <template v-if="!agentBrokerEnabled">
      <header class="page-heading">
        <div><p class="eyebrow">{{ t('agent.eyebrow') }}</p><h1>{{ t('agent.title') }}</h1></div>
      </header>
      <section class="agent-placeholder" data-agent-disabled>
        <h2>{{ t('agent.disabledTitle') }}</h2>
        <p>{{ t('agent.disabledDesc') }}</p>
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
import { useI18n } from '../i18n'

const { t } = useI18n()
const route = useRoute()
const conversationId = computed(() => String(route.params.conversationId))

const { agentBrokerEnabled } = useAgentBroker()
</script>
