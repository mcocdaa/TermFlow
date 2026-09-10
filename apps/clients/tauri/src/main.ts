import '@termflow/client-ui/styles'
import { platform } from '@tauri-apps/plugin-os'
import { App, createClientUi } from '@termflow/client-ui'
import { createApp } from 'vue'
import { createTauriRouter } from './router'
import { createTauriRuntime } from './runtime'
import { createTauriThemeState } from './adapters/tauriTheme'
import { setTauriPlatformAttribute } from './tauriPlatformAttribute'

async function start() {
  setTauriPlatformAttribute(document.documentElement, platform())
  const runtime = await createTauriRuntime()
  const router = createTauriRouter(runtime)
  const theme = await createTauriThemeState()
  createApp(App).use(router).use(createClientUi(runtime, { theme })).mount('#app')
}

void start()
