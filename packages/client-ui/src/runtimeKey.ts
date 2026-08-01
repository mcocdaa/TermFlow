import type { InjectionKey } from 'vue'
import type { ClientRuntime } from './runtime'

export const clientRuntimeKey: InjectionKey<ClientRuntime> = Symbol('termflow-client-runtime')
