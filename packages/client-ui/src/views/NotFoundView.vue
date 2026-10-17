<template><section><h1>{{ t('notFound.title') }}</h1><RouterLink to="/">{{ t('notFound.back') }}</RouterLink></section></template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useI18n } from '../i18n'
const { t } = useI18n()
</script>
