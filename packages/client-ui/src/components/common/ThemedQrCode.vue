<template>
  <img v-if="source" class="themed-qr-code" :src="source" :alt="alt" />
</template>

<script setup lang="ts">
import QRCode from 'qrcode'
import { ref, watch } from 'vue'
import { useTheme } from '../../theme/theme'

const props = defineProps<{ value: string; alt: string }>()
const theme = useTheme()
const source = ref('')
let renderSequence = 0

async function render() {
  const sequence = ++renderSequence
  const styles = getComputedStyle(document.documentElement)
  const foreground = styles.getPropertyValue('--color-qr-foreground').trim()
  const background = styles.getPropertyValue('--color-qr-background').trim()
  const svg = await QRCode.toString(props.value, {
    type: 'svg',
    errorCorrectionLevel: 'M',
    margin: 2,
    color: { dark: foreground, light: background },
  })
  if (sequence === renderSequence) {
    source.value = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  }
}

watch([() => props.value, theme.active], () => { void render() }, { immediate: true })
</script>
