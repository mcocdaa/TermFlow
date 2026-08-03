<template>
  <div data-totp-protection-label class="security-setting-label">
    <strong>启用双重认证登录</strong>
    <span class="help-tooltip">
      <button
        data-action="explain-totp-protection"
        class="icon-button icon-only help-tooltip-trigger"
        type="button"
        aria-label="说明启用双重认证登录"
        :aria-describedby="tooltipId"
      >
        <CircleHelp :size="17" aria-hidden="true" />
      </button>
      <span :id="tooltipId" role="tooltip">启用后，新的管理员登录和客户端授权都需要验证器生成的一次性验证码。</span>
    </span>
  </div>
</template>

<script setup lang="ts">
import { CircleHelp } from '@lucide/vue'

const tooltipId = `totp-protection-help-${crypto.randomUUID()}`
</script>
