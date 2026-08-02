import { createApp } from 'vue'
import { createClientUi } from '@termflow/client-ui'
import '@termflow/design-tokens/styles'
import '@xterm/xterm/css/xterm.css'
import './styles/reset.css'
import './styles/app.css'
import './styles/terminal-responsive.css'
import App from './App.vue'
import { createAppRouter } from './router'
import { applyInitialTheme } from './stores/theme'
import { refreshSession } from './stores/session'
import { browserRuntime } from './runtime'

applyInitialTheme()

const router = createAppRouter({
  sessionStatus: refreshSession,
})

createApp(App).use(createClientUi(browserRuntime)).use(router).mount('#app')
