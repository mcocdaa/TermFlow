<template>
  <div class="agent-collapsible-output" data-agent-collapsible-output>
    <!-- Diff mode -->
    <div v-if="isDiff" class="agent-diff-viewer" data-agent-diff-viewer>
      <template v-if="!isLong || expanded">
        <div
          v-for="(line, idx) in diffLines"
          :key="idx"
          class="agent-diff-line"
          :class="`agent-diff-line--${line.type}`"
        >
          <span class="agent-diff-line__text">{{ line.content }}</span>
        </div>
        <button
          v-if="isLong"
          type="button"
          class="agent-fold-toggle agent-fold-toggle--collapse"
          data-action="toggle-fold"
          @click="expanded = false"
        >
          收起代码 ▴
        </button>
      </template>
      <template v-else>
        <!-- Collapsed middle mode -->
        <div
          v-for="(line, idx) in diffLines.slice(0, headCount)"
          :key="'h-' + idx"
          class="agent-diff-line"
          :class="`agent-diff-line--${line.type}`"
        >
          <span class="agent-diff-line__text">{{ line.content }}</span>
        </div>
        <button
          type="button"
          class="agent-fold-toggle agent-fold-toggle--expand"
          data-action="toggle-fold"
          @click="expanded = true"
        >
          展开其余 {{ foldedCount }} 行 (共 {{ totalLines }} 行) ▾
        </button>
        <div
          v-for="(line, idx) in diffLines.slice(diffLines.length - tailCount)"
          :key="'t-' + idx"
          class="agent-diff-line"
          :class="`agent-diff-line--${line.type}`"
        >
          <span class="agent-diff-line__text">{{ line.content }}</span>
        </div>
      </template>
    </div>

    <!-- Plain text / code mode -->
    <div v-else class="agent-output-block" :class="codeClass">
      <template v-if="!isLong || expanded">
        <pre class="agent-output-pre">{{ cleanContent }}</pre>
        <button
          v-if="isLong"
          type="button"
          class="agent-fold-toggle agent-fold-toggle--collapse"
          data-action="toggle-fold"
          @click="expanded = false"
        >
          收起输出 ▴
        </button>
      </template>
      <template v-else>
        <pre class="agent-output-pre">{{ headContent }}</pre>
        <button
          type="button"
          class="agent-fold-toggle agent-fold-toggle--expand"
          data-action="toggle-fold"
          @click="expanded = true"
        >
          展开其余 {{ foldedCount }} 行 (共 {{ totalLines }} 行) ▾
        </button>
        <pre class="agent-output-pre">{{ tailContent }}</pre>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { stripAnsiOsc } from '@termflow/client-core'
import { isUnifiedDiff, parseUnifiedDiff, type DiffLine } from '../../utils/diff'

const props = withDefaults(
  defineProps<{
    content: string
    status?: string | null | undefined
    maxCollapsedLines?: number
    codeClass?: string
  }>(),
  {
    status: null,
    maxCollapsedLines: 10,
    codeClass: '',
  },
)

const cleanContent = computed(() => stripAnsiOsc(props.content ?? ''))
const rawLines = computed(() => cleanContent.value.split('\n'))
const totalLines = computed(() => rawLines.value.length)
const isLong = computed(() => totalLines.value > props.maxCollapsedLines)
const isError = computed(() => props.status === 'error' || props.status === 'failed')

// Auto-expand on error/failed
const expanded = ref(isError.value)
watch(isError, (err) => {
  if (err) expanded.value = true
})

const headCount = 5
const tailCount = 3
const foldedCount = computed(() => Math.max(0, totalLines.value - headCount - tailCount))

const headContent = computed(() => rawLines.value.slice(0, headCount).join('\n'))
const tailContent = computed(() => rawLines.value.slice(totalLines.value - tailCount).join('\n'))

const isDiff = computed(() => isUnifiedDiff(cleanContent.value))
const diffLines = computed<DiffLine[]>(() => isDiff.value ? parseUnifiedDiff(cleanContent.value) : [])
</script>
