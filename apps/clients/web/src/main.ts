import { createApp } from 'vue'
import { App, createClientUi, createThemeState } from '@termflow/client-ui'
import '@termflow/client-ui/styles'
import { createBrowserThemePreferences, createBrowserThemeTarget } from './adapters/browserThemePreferences'
import { createAppRouter } from './router'
import { browserRuntime } from './runtime'

const theme = createThemeState(createBrowserThemePreferences(), createBrowserThemeTarget())
const clientUi = createClientUi(browserRuntime, { theme })

const router = createAppRouter({
  sessionStatus: clientUi.session.refreshSession,
})

createApp(App).use(clientUi).use(router).mount('#app')
