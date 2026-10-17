<template>
  <div class="name-editor">
    <template v-if="editing">
      <label class="sr-only" :for="`computer-name-${computerId}`">{{ t('computers.nameEditor.label') }}</label>
      <input :id="`computer-name-${computerId}`" v-model="draft" name="display-name" maxlength="128" @keydown.esc="cancel" @keydown.enter.prevent="save" />
      <button data-action="save-name" class="primary-button compact" type="button" :disabled="busy" @click="save">{{ t('common.save') }}</button>
      <button class="text-button compact" type="button" @click="cancel">{{ t('common.cancel') }}</button>
    </template>
    <button v-else data-action="edit-name" class="name-edit-trigger" type="button" :aria-label="t('computers.nameEditor.editAria', { name: currentName })" :title="t('computers.nameEditor.editTitle')" @click="begin"><strong>{{ currentName }}</strong></button>
    <p v-if="message" role="alert" class="form-error">{{ message }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ApiError } from '@termflow/client-core'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'

const { t } = useI18n()
const props = defineProps<{ computerId: string; displayName: string }>()
const emit = defineEmits<{ updated: [name: string] }>()
const runtime = useClientRuntime()
const editing = ref(false)
const busy = ref(false)
const draft = ref(props.displayName)
const currentName = ref(props.displayName)
const message = ref('')
watch(() => props.displayName, (value) => { currentName.value = value; if (!editing.value) draft.value = value })

function validName(value: string) {
  return value.length >= 1 && value.length <= 128 && !/[\u0000-\u001f\u007f]/.test(value)
}
function begin() { draft.value = currentName.value; message.value = ''; editing.value = true }
function cancel() { draft.value = currentName.value; message.value = ''; editing.value = false }
async function save() {
  if (!validName(draft.value)) { message.value = t('computers.nameEditor.invalid'); return }
  const previous = currentName.value
  currentName.value = draft.value
  editing.value = false
  busy.value = true
  try {
    const updated = await runtime.api.computers.rename(props.computerId, draft.value)
    currentName.value = updated.display_name
    emit('updated', updated.display_name)
  } catch (error) {
    currentName.value = previous
    editing.value = true
    message.value = error instanceof ApiError ? error.message : t('computers.nameEditor.saveError')
  } finally { busy.value = false }
}
</script>
