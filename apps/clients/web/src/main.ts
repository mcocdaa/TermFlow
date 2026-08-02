import { createApp } from 'vue'
import { App, configureActiveTheme, createClientUi, createSessionActions } from '@termflow/client-ui'
import '@termflow/design-tokens/styles'
import '@termflow/client-ui/styles/reset.css'
import '@termflow/client-ui/styles/app.css'
import '@termflow/client-ui/styles/terminal-responsive.css'
import '@xterm/xterm/css/xterm.css'
import { createBrowserThemePreferences, createBrowserThemeTarget } from './adapters/browserThemePreferences'
import { createAppRouter } from './router'
import { browserRuntime } from './runtime'

configureActiveTheme(createBrowserThemePreferences(), createBrowserThemeTarget())

const sessionActions = createSessionActions(browserRuntime.api)
const router = createAppRouter({
  sessionStatus: sessionActions.refreshSession,
})

createApp(App).use(createClientUi(browserRuntime)).use(router).mount('#app')
