<template>
  <div data-totp-protection-label class="security-setting-label">
    <strong>启用双重认证登录</strong>
    <ContextHelp
      data-action="explain-totp-protection"
      label="说明启用双重认证登录"
      text="启用后，新的管理员登录和客户端授权都需要验证器生成的一次性验证码。"
    />
  </div>
</template>

<script setup lang="ts">
import ContextHelp from '../common/ContextHelp.vue'
</script>
