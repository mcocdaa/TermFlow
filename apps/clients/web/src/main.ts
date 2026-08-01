import { createApp } from 'vue'
import App from './App.vue'
import { createAppRouter } from './router'

const router = createAppRouter({
  sessionStatus: async () => {
    const response = await fetch('/api/v1/session', { credentials: 'same-origin' })
    return { authenticated: response.ok }
  },
})

createApp(App).use(router).mount('#app')
