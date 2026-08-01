import { createApp } from 'vue'
import '@termflow/design-tokens/styles'
import './styles/reset.css'
import './styles/app.css'
import App from './App.vue'
import { createAppRouter } from './router'
import { applyInitialTheme } from './stores/theme'
import { refreshSession } from './stores/session'

applyInitialTheme()

const router = createAppRouter({
  sessionStatus: refreshSession,
})

createApp(App).use(router).mount('#app')
