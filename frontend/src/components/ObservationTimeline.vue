<template>
  <section class="timeline-panel">
    <div class="trace-controls">
      <div class="filters" aria-label="调用筛选"><button v-for="item in filters" :key="item.value" :class="{ chosen: filter === item.value }" :aria-pressed="filter === item.value" @click="filter = item.value">{{ item.label }}</button></div>
      <input v-model="query" aria-label="搜索调用" placeholder="搜索步骤或工具名称…">
      <div class="expand"><button @click="expanded = [...forest.nodes.keys()]">全部展开</button><button @click="expanded = []">收起</button></div>
    </div>
    <p class="trace-note">{{ rows.length }} 条可见 · 共 {{ spans.length }} 条记录 · 优先自动选择最慢的已计时模型调用 <span v-if="filter === 'slow'"> · 慢调用筛选 ≥10秒，非性能达标线</span></p>
    <div v-if="!rows.length" class="empty">{{ spans.length ? '没有匹配的调用' : '尚无可用步骤记录' }}<button v-if="spans.length" @click="filter = 'all'; query = ''">清空筛选</button></div>
    <div v-else ref="scrollElement" class="timeline-scroll">
      <div class="timeline">
        <div class="axis"><div class="axis-label">执行步骤 <span>持续时间</span></div><div class="axis-scale"><span v-for="position in [0, 25, 50, 75, 100]" :key="position" :style="{ left: position + '%' }">{{ axisLabel(position) }}</span></div></div>
        <div v-for="row in rows" :key="row.node.span.span_id" class="trace-row" :class="{ selected: selectedId === row.node.span.span_id, context: row.context }" :data-span-id="row.node.span.span_id">
          <div class="row-label" :style="{ paddingLeft: Math.min(row.depth, 8) * 13 + 9 + 'px' }">
            <button v-if="row.node.children.length" class="toggle" :aria-expanded="expanded.includes(row.node.span.span_id)" :aria-label="`展开或收起${stepName(row.node.span.name)}`" @click="toggle(row.node.span.span_id)">{{ expanded.includes(row.node.span.span_id) ? '▾' : '▸' }}</button><span v-else class="toggle"></span>
            <button class="call-label" :aria-pressed="selectedId === row.node.span.span_id" @click="$emit('select', row.node.span.span_id)">
              <span class="call-title"><i :class="kind(row.node.span)"></i>{{ stepName(row.node.span.name) }}<small v-if="row.node.warning || timingIssue(row.node.span)" :title="row.node.warning || timingIssue(row.node.span)">⚠</small></span>
              <span class="call-meta">{{ spanState(row.node.span, active) }} · {{ spanTime(row.node.span, active, now) }}<span v-if="timingIssue(row.node.span)"> · {{ timingIssue(row.node.span) }}</span></span>
            </button>
          </div>
          <div class="track">
            <button class="bar" :class="[kind(row.node.span), { pending: row.node.span.status === 'running' && active, unknown: durationMs(row.node.span) === null && !(row.node.span.status === 'running' && active) }]" :style="barStyle(row.node.span)" :title="`${stepName(row.node.span.name)} · ${spanState(row.node.span, active)} · ${spanTime(row.node.span, active, now)}${row.node.warning ? ' · ' + row.node.warning : ''}`" :aria-label="`查看${stepName(row.node.span.name)}，${spanState(row.node.span, active)}，${spanTime(row.node.span, active, now)}`" @click="$emit('select', row.node.span.span_id)"><span v-if="durationMs(row.node.span) === null">{{ row.node.span.status === 'running' && active ? '执行中' : '?' }}</span></button>
          </div>
        </div>
      </div>
    </div>
    <footer><span><i class="llm"></i>模型</span><span><i class="tool"></i>工具</span><span><i class="stage"></i>步骤</span><span><i class="failed"></i>异常</span><span>虚线：结束时间未知；短条有最小点击宽度</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch, nextTick, onMounted } from 'vue'
import type { SpanSummary, TaskStatus } from '@/types'
import { stepName } from '@/services/api'
import { ancestors, buildForest, defaultExpanded, durationMs, isActive, spanState, spanTime, time, timingIssue, timelineBounds, traceRows, type TraceFilter } from '@/services/observationView'
const props = defineProps<{ spans: SpanSummary[]; task: TaskStatus; selectedId: string; now: number }>()
defineEmits<{ select: [id: string] }>()
const filter = ref<TraceFilter>('all'), query = ref(''), expanded = ref<string[]>([]), scrollElement = ref<HTMLElement | null>(null)
const filters: { value: TraceFilter; label: string }[] = [{ value: 'all', label: '全部' }, { value: 'model', label: '模型' }, { value: 'errors', label: '异常' }, { value: 'slow', label: '慢调用' }]
const forest = computed(() => buildForest(props.spans)), active = computed(() => isActive(props.task))
let initialized = false
watch(forest, value => {
  if (!initialized && value.nodes.size) { expanded.value = defaultExpanded(value); initialized = true }
  reveal()
}, { immediate: true })
watch(() => props.selectedId, () => { reveal(); void nextTick(scrollSelection) })
onMounted(() => { void nextTick(scrollSelection) })
function scrollSelection() {
  const container = scrollElement.value, row = container?.querySelector<HTMLElement>('.trace-row.selected')
  if (!container || !row) return
  const box = container.getBoundingClientRect(), target = row.getBoundingClientRect()
  if (target.top < box.top + 36) container.scrollTop -= box.top + 36 - target.top
  else if (target.bottom > box.bottom) container.scrollTop += target.bottom - box.bottom
}
function reveal() { expanded.value = [...new Set([...expanded.value, ...ancestors(forest.value, props.selectedId)])] }
const rows = computed(() => traceRows(forest.value, new Set(expanded.value), filter.value, query.value, stepName))
const bounds = computed(() => timelineBounds(props.task, props.spans, props.now))
function toggle(id: string) { expanded.value = expanded.value.includes(id) ? expanded.value.filter(x => x !== id) : [...expanded.value, id] }
function kind(s: SpanSummary) { return ['failed', 'interrupted'].includes(s.status) ? 'failed' : ['llm', 'tool'].includes(s.operation_type) ? s.operation_type : 'stage' }
function axisLabel(position: number) { return `${((bounds.value.left + bounds.value.length * position / 100 - bounds.value.origin) / 1000).toFixed(0)}s` }
function barStyle(s: SpanSummary) {
  const start = time(s.started_at), duration = durationMs(s)
  if (start === null) return { left: '0%', width: '8px' }
  const elapsed = s.status === 'running' && active.value ? Math.max(0, props.now - start) : duration ?? 0
  return { left: `min(calc(100% - 8px), ${Math.max(0, (start - bounds.value.left) / bounds.value.length * 100)}%)`, width: `max(8px, ${elapsed / bounds.value.length * 100}%)` }
}
</script>
<style scoped>
.timeline-panel{min-width:0}.trace-controls{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:16px 18px 6px}.filters{display:flex;padding:3px;background:#f0f3f7;border-radius:7px}.filters button,.expand button{border:0;background:transparent;padding:6px 10px;color:#657086;cursor:pointer;border-radius:5px}.filters .chosen{background:white;color:#176b56;box-shadow:0 1px 4px #1a2d3b17}.trace-controls input{border:1px solid #dde4e9;border-radius:6px;padding:7px 10px;flex:1;min-width:150px;width:150px}.expand{display:flex}.trace-note{font-size:12px;color:#778395;margin:5px 18px 12px}.timeline-scroll{overflow:auto;max-height:640px;border-top:1px solid #edf0f3}.timeline{min-width:660px}.axis,.trace-row{display:grid;grid-template-columns:265px minmax(0,1fr)}.axis{position:sticky;top:0;background:#f8fafc;z-index:3;height:36px;color:#7d899a;font-size:11px;border-bottom:1px solid #e7edf1}.axis-label{padding:10px 12px;display:flex;justify-content:space-between}.axis-scale{position:relative;margin-right:24px}.axis-scale span{position:absolute;top:10px;transform:translateX(-50%)}.axis-scale span:first-child{transform:none}.axis-scale span:last-child{transform:translateX(-100%)}.trace-row{height:53px;border-bottom:1px solid #eef1f4}.trace-row:hover{background:#f7faf9}.trace-row.selected{background:#eaf5f0;box-shadow:inset 3px 0 #21866b}.row-label{display:flex;align-items:center;overflow:hidden;border-right:1px solid #e8edf1}.toggle{width:18px;flex-shrink:0;border:0;background:none;color:#83909e;padding:0;cursor:pointer}.call-label{border:0;background:transparent;text-align:left;min-width:0;flex:1;cursor:pointer;padding:5px 8px 5px 0}.call-title{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:#354357;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.call-meta{font-size:10px;color:#7b8796;display:block;margin:4px 0 0 14px}.context .call-title{font-weight:400;color:#8893a0}.track{position:relative;margin-right:24px;background:repeating-linear-gradient(to right,transparent 0,transparent calc(25% - 1px),#eef1f4 calc(25% - 1px),#eef1f4 25%)}.bar{position:absolute;top:18px;height:16px;border:0;border-radius:4px;cursor:pointer;max-width:100%;color:white;font-size:9px;padding:0;overflow:hidden;min-width:8px}.llm{background:#279d7c}.tool{background:#5f9bda}.stage{background:#a591c7}.failed{background:#df7a75}.pending{background-image:repeating-linear-gradient(135deg,transparent,transparent 4px,#ffffff55 4px,#ffffff55 7px)}.unknown{border:1px dashed currentColor;background:transparent;color:#9a6e63;height:14px}i{display:inline-block;width:7px;height:7px;border-radius:2px;flex-shrink:0}footer{display:flex;flex-wrap:wrap;gap:14px;padding:13px 18px;font-size:11px;color:#7d899a}footer span{display:flex;align-items:center;gap:5px}.empty{text-align:center;padding:70px 20px;color:#7d899a}.empty button{display:block;margin:15px auto;border:1px solid #dde4e9;background:white;padding:6px 12px;border-radius:6px;cursor:pointer}button:focus-visible,input:focus-visible{outline:2px solid #21866b;outline-offset:2px}
</style>
