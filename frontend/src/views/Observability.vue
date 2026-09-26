<template>
  <main class="observatory">
    <header><div><h1>规划观测</h1><p>查看真实执行步骤、模型消息和工具结果</p></div><router-link to="/">返回旅行规划</router-link></header>
    <a-alert v-if="error" :message="error" type="error" show-icon style="margin-bottom: 16px" />
    <section class="workspace">
      <aside>
        <h2>任务记录</h2>
        <a-space wrap>
          <a-select v-model:value="filter" style="width: 130px" @change="resetList">
            <a-select-option value="">全部状态</a-select-option>
            <a-select-option v-for="state in states" :key="state" :value="state">{{ stateName(state) }}</a-select-option>
          </a-select>
          <a-button @click="refresh">刷新</a-button>
        </a-space>
        <p v-if="!tasks.length" class="muted">暂无任务记录</p>
        <button v-for="task in tasks" :key="task.task_id" class="task" :class="{ selected: selectedId === task.task_id }" @click="select(task.task_id)">
          <strong>{{ stateName(task.status) }}</strong><span>{{ dateText(task.created_at) }}</span><small>{{ task.task_id }}</small>
          <span v-if="task.observation_incomplete">记录不完整</span>
        </button>
        <a-space><a-button :disabled="offset === 0" @click="page(-50)">上一页</a-button><a-button :disabled="tasks.length < 50" @click="page(50)">下一页</a-button></a-space>
      </aside>
      <article v-if="selected">
        <h2>{{ stateName(selected.status) }} <small>{{ selected.task_id }}</small></h2>
        <p v-if="selected.current_step">{{ stepName(selected.current_step.name) }} · 已等待 {{ duration(selected.current_step.started_at, null) }}</p>
        <a-alert v-if="selected.observation_incomplete" type="warning" message="观测记录不完整，缺少步骤不代表没有执行。" show-icon />
        <a-alert v-if="selected.persistence_error" type="error" :message="selected.persistence_error.message + '；终态尚未持久化'" show-icon />
        <p v-if="selected.error_code">{{ selected.error_code }} · {{ selected.error_message }} <span v-if="selected.error_step">（{{ stepName(selected.error_step) }}）</span></p>
        <p v-if="selected.status === 'interrupted'">发现中断：{{ dateText(selected.interruption_detected_at) }}；实际结束时间未知。</p>
        <a-button v-if="selected.status === 'succeeded'" @click="showResult">查看行程结果</a-button>
        <div class="trace">
          <section><h3>调用树</h3><p v-if="!spans.length" class="muted">尚无可用步骤记录</p>
            <a-tree v-if="tree.length" :tree-data="tree" :expanded-keys="expanded" @expand="(keys: any) => expanded = keys" @select="chooseSpan" block-node />
          </section>
          <section class="details"><h3>步骤详情</h3><p v-if="!detail" class="muted">点击步骤查看输入和输出。</p>
            <template v-if="detail">
              <h4>{{ stepName(detail.name) }} · {{ stateName(detail.status) }}</h4>
              <p>{{ detail.name }} · {{ duration(detail.started_at, detail.finished_at, detail.status) }}</p>
              <a-button size="small" @click="loadDetail(detail.span_id)">刷新详情</a-button>
              <p v-if="detail.input_truncated || detail.output_truncated" class="warning">内容超出大小限制，已截断。</p>
              <h4>输入</h4><pre>{{ pretty(detail.input_data) }}</pre>
              <h4>输出</h4><pre>{{ pretty(detail.output_data) }}</pre>
              <template v-if="detail.error"><h4>错误</h4><pre>{{ pretty(detail.error) }}</pre></template>
            </template>
          </section>
        </div>
      </article>
      <article v-else><a-empty description="选择一个任务查看执行过程" /></article>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listTasks, getTask, getSpans, getSpan, getTaskResult, errorText, stepName, stateName } from '@/services/api'
import type { TaskStatus, SpanSummary, SpanDetail } from '@/types'
const route = useRoute(), router = useRouter()
const states = ['accepted', 'running', 'succeeded', 'failed', 'interrupted']
const tasks = ref<TaskStatus[]>([]), selected = ref<TaskStatus | null>(null), spans = ref<SpanSummary[]>([])
const selectedId = ref(''), detail = ref<SpanDetail | null>(null), error = ref(''), filter = ref(''), offset = ref(0)
const expanded = ref<string[]>([]), tick = ref(Date.now())
let timer: ReturnType<typeof setTimeout> | undefined, clock: ReturnType<typeof setInterval> | undefined
let disposed = false, generation = 0, detailGeneration = 0
const active = (task: TaskStatus) => task.status === 'accepted' || task.status === 'running'
const dateText = (value: string | null) => value ? new Date(value).toLocaleString() : '未知'
const pretty = (value: unknown) => value == null ? '无可用内容' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
function duration(start: string, end: string | null, status = 'running') {
  if (!end && status !== 'running') return '实际结束时间未知'
  return `${Math.max(0, Math.floor(((end ? Date.parse(end) : tick.value) - Date.parse(start)) / 1000))} 秒`
}
interface TreeNode { key: string; title: string; children: TreeNode[] }
const tree = computed(() => {
  const nodes = new Map<string, TreeNode>()
  spans.value.forEach(s => nodes.set(s.span_id, { key: s.span_id, title: `${stepName(s.name)} · ${stateName(s.status)} · ${duration(s.started_at, s.finished_at, s.status)}`, children: [] }))
  const roots: TreeNode[] = []
  spans.value.forEach(s => {
    const node = nodes.get(s.span_id)!
    const parent = s.parent_span_id ? nodes.get(s.parent_span_id) : null
    if (parent) parent.children.push(node)
    else roots.push(node)
  })
  return roots
})
async function refresh() {
  clearTimeout(timer)
  const version = ++generation
  try {
    const rows = await listTasks(filter.value, offset.value)
    if (disposed || version !== generation) return
    tasks.value = rows
    if (selectedId.value) {
      const [task, records] = await Promise.all([getTask(selectedId.value), getSpans(selectedId.value)])
      if (disposed || version !== generation) return
      selected.value = task; spans.value = records
      const existing = new Set(expanded.value)
      records.filter(s => s.parent_span_id === null || s.operation_type === 'agent').forEach(s => existing.add(s.span_id))
      expanded.value = [...existing]
    }
    error.value = ''
  } catch (e: any) {
    if (disposed || version !== generation) return
    error.value = `查询失败：${errorText(e)}（不代表规划失败）`
    if (e.response?.status === 404) { selected.value = null; selectedId.value = ''; spans.value = []; detail.value = null }
  }
  if (!disposed && version === generation && (error.value || tasks.value.some(active) || (selected.value && active(selected.value)))) timer = setTimeout(refresh, 2000)
}
function select(id: string) {
  selectedId.value = id; selected.value = null; detail.value = null; spans.value = []; expanded.value = []
  detailGeneration++
  void router.replace({ query: { task: id } })
  void refresh()
}
async function loadDetail(id: string) {
  const version = ++detailGeneration, task = selectedId.value
  try {
    const value = await getSpan(task, id)
    if (!disposed && version === detailGeneration && task === selectedId.value) detail.value = value
  } catch (e) { if (!disposed && version === detailGeneration) error.value = errorText(e) }
}
function chooseSpan(keys: (string | number)[]) { if (keys[0]) void loadDetail(String(keys[0])) }
async function showResult() {
  try {
    const response = await getTaskResult(selectedId.value)
    sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
    await router.push('/result')
  } catch (e) { error.value = errorText(e) }
}
function resetList() { offset.value = 0; void refresh() }
function page(delta: number) { offset.value += delta; void refresh() }
onMounted(() => {
  selectedId.value = typeof route.query.task === 'string' ? route.query.task : ''
  void refresh(); clock = setInterval(() => { tick.value = Date.now() }, 1000)
})
onUnmounted(() => { disposed = true; clearTimeout(timer); clearInterval(clock) })
</script>

<style scoped>
.observatory { max-width: 1500px; margin: auto; padding: 32px; color: #17243b; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
h1 { margin: 0; } header p, .muted { color: #65738a; }
.workspace { display: grid; grid-template-columns: 290px minmax(0, 1fr); gap: 24px; }
aside, article { background: white; border: 1px solid #dde4ed; border-radius: 12px; padding: 20px; min-width: 0; }
.task { display: flex; flex-direction: column; text-align: left; width: 100%; margin: 12px 0; padding: 12px; border: 1px solid #e1e6ef; background: #fafbfe; border-radius: 8px; cursor: pointer; gap: 5px; overflow-wrap: anywhere; }
.task.selected { border-color: #346ac5; background: #edf4ff; } small { font-weight: normal; font-size: 11px; overflow-wrap: anywhere; }
.trace { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(0, 1.3fr); gap: 20px; margin-top: 24px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 450px; overflow: auto; padding: 14px; background: #f4f6fa; border-radius: 8px; font-size: 12px; }
.warning { color: #946000; }
@media(max-width: 900px) { .workspace, .trace { grid-template-columns: 1fr; } .observatory { padding: 16px; } }
</style>
