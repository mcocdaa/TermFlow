import type { InjectionKey } from 'vue'
import type { SessionActions } from './composables/useSession'
import type { BottomToastController } from './composables/useBottomToast'
import type { ClientRuntime } from './runtime'
import type { ThemeState } from './theme/theme'

export const clientRuntimeKey: InjectionKey<ClientRuntime> = Symbol('termflow-client-runtime')
export const sessionActionsKey: InjectionKey<SessionActions> = Symbol('termflow-session-actions')
export const themeStateKey: InjectionKey<ThemeState> = Symbol('termflow-theme-state')
export const bottomToastKey: InjectionKey<BottomToastController> = Symbol('termflow-bottom-toast')
