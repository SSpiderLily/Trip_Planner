<template>
  <RouteResultEditable v-if="raw?.schema_version === 3" :initial-plan="raw" :task-id="taskId" />
  <RouteResultLegacy v-else-if="raw?.schema_version === 2" />
  <main v-else class="legacy-result">
    <router-link to="/">← 返回首页</router-link>
    <h1>历史行程 · 只读数据</h1>
    <p>这份结果使用旧格式，路线未经过新版检查；费用按旧版原始口径只读展示。可以返回首页重新生成。</p>
    <pre>{{ JSON.stringify(raw, null, 2) }}</pre>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import RouteResultEditable from './RouteResultEditable.vue'
import RouteResultLegacy from './RouteResultLegacy.vue'

const raw = ref<any>(null)
try { raw.value = JSON.parse(sessionStorage.getItem('tripPlan') || 'null') } catch { /* 损坏的缓存不阻断导航 */ }
const taskId = sessionStorage.getItem('tripTaskId') || ''
</script>

<style scoped>
.legacy-result { max-width: 1000px; margin: auto; padding: 32px; color: #203a35; }
pre { max-height: 70vh; overflow: auto; padding: 20px; white-space: pre-wrap; overflow-wrap: anywhere; background: #f5f5f2; border-radius: 12px; }
</style>
