import {
  MobileModifierController as CoreMobileModifierController,
  createModifierState,
} from '@termflow/client-core'
import { reactive } from 'vue'

export { keyNotationBytes } from '@termflow/client-core'
export type { ModifierKey, ModifierMode, ModifierState } from '@termflow/client-core'

export class MobileModifierController extends CoreMobileModifierController {
  constructor() { super(reactive(createModifierState())) }
}
