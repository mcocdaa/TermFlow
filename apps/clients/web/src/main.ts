import { createApp } from 'vue'
import '@termflow/design-tokens/styles'
import './styles/reset.css'
import './styles/app.css'
import App from './App.vue'
import { createAppRouter } from './router'
import { applyInitialTheme } from './stores/theme'

applyInitialTheme()

const router = createAppRouter({
  sessionStatus: async () => {
    const response = await fetch('/api/v1/session', { credentials: 'same-origin' })
    return { authenticated: response.ok }
  },
})

createApp(App).use(router).mount('#app')
