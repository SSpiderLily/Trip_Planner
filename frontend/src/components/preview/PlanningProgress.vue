<template>
  <div class="planning-progress">
    <span class="loading-caption">当前阶段</span>
    <p class="loading-stage" role="status" aria-live="polite" aria-atomic="true">{{ label }}</p>
    <p class="loading-note">地点安排完成后会打开预览，交通查询会继续进行。</p>
    <router-link v-if="observationTarget" class="observation-link" :to="observationTarget">查看当前任务的规划过程 ↗</router-link>
  </div>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import { v4PreviewStageLabel } from '@/utils/v4PreviewProgress'
import { planningObservationTarget } from '@/utils/v4PlanningSession'
const props = defineProps<{ stage: string; status: string; taskId: string | null }>()
const label = computed(() => v4PreviewStageLabel(props.stage, props.status))
const observationTarget = computed(() => planningObservationTarget(props.taskId))
</script>
<style scoped>
.loading-caption{font-size:12px;color:#7c8b7e}.loading-stage{font-size:18px;color:#34563e;line-height:1.7}.loading-note{font-size:13px;color:#718173;line-height:1.7}.observation-link{display:inline-block;margin-top:14px;color:#355b44;text-decoration:underline;text-underline-offset:4px}.observation-link:focus-visible{outline:2px solid #355b44;outline-offset:4px}
</style>
