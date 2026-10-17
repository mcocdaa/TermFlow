<template>
  <div data-totp-protection-label class="security-setting-label">
    <strong>{{ t('settings.totp.protectionLabel') }}</strong>
    <ContextHelp
      data-action="explain-totp-protection"
      :label="t('settings.totp.protectionHelpLabel')"
      :text="t('settings.totp.protectionHelpText')"
    />
  </div>
</template>

<script setup lang="ts">
import ContextHelp from '../common/ContextHelp.vue'
import { useI18n } from '../../i18n'
const { t } = useI18n()
</script>
