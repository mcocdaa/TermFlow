<template>
  <section class="login-view">
    <div class="auth-card">
      <h1>登录</h1>
      <form @submit.prevent="submit">
        <label for="admin-token">管理员令牌</label>
        <input id="admin-token" v-model="adminToken" type="password" autocomplete="off" required />
        <p v-if="message" role="alert" class="form-error">{{ message }}</p>
        <button class="primary-button" type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/http'
import { loginWithToken } from '../stores/session'

const adminToken = ref('')
const busy = ref(false)
const message = ref('')
const route = useRoute()
const router = useRouter()

async function submit() {
  if (!adminToken.value || busy.value) return
  busy.value = true
  message.value = ''
  const token = adminToken.value
  adminToken.value = ''
  try {
    await loginWithToken(token)
    const requested = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(requested.startsWith('/') && !requested.startsWith('//') ? requested : '/')
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : '登录失败，请重试。'
  } finally {
    busy.value = false
  }
}
</script>
