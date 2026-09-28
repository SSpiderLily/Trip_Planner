<template>
  <main class="observatory">
    <header><div><h1>规划观测</h1><p>查看单任务工程指标、真实执行步骤和调用详情</p></div><router-link to="/">返回旅行规划</router-link></header>
    <a-alert v-if="error" :message="error" type="error" show-icon style="margin-bottom: 16px" />
    <section class="workspace">
      <aside>
        <h2>任务记录</h2>
        <a-space wrap>
          <a-select v-model:value="filter" style="width: 130px" @change="resetList">
            <a-select-option value="">全部状态</a-select-option>
            <a-select-option v-for="state in states" :key="state" :value="state">{{ stateName(state) }}</a-select-option>
          </a-select>
          <a-button @click="refresh(true)">刷新</a-button>
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
        <nav class="views" aria-label="观测视图">
          <button type="button" :class="{ current: view === 'metrics' }" :aria-current="view === 'metrics' ? 'page' : undefined" @click="setView('metrics')">工程指标</button>
          <button type="button" :class="{ current: view === 'trace' }" :aria-current="view === 'trace' ? 'page' : undefined" @click="setView('trace')">调用记录</button>
        </nav>
        <section v-if="view === 'metrics'" class="metrics">
          <a-alert v-if="active(selected)" type="info" message="任务结束后可查看工程指标。调用记录仍会更新。" show-icon />
          <a-alert v-else-if="metricError" type="error" :message="`指标查询失败：${metricError}（不代表规划失败）`" show-icon>
            <template #action><a-button size="small" @click="loadMetrics(true)">重试</a-button></template>
          </a-alert>
          <a-spin v-else-if="metricLoading" tip="正在读取工程指标…" />
          <template v-else-if="metrics">
            <p class="muted">工程指标反映本次规划的执行情况，不代表行程业务质量。调用次数仅包含已记录的外部模型和工具调用。</p>
            <div class="cards">
              <section class="metric-card"><h3>任务概况</h3><strong>{{ stateName(metrics.task.status) }}</strong><p>总耗时：{{ milliseconds(metrics.task.duration_ms) }}</p><p>记录完整性：{{ metrics.task.observation_incomplete ? '记录不完整' : '未发现缺失' }}</p><p v-if="metrics.task.error_code">失败阶段：{{ metrics.task.error_step ? stepName(metrics.task.error_step) : '未知' }}<br>{{ metrics.task.error_code }} · {{ metrics.task.error_message }}</p></section>
              <section class="metric-card"><h3>模型调用</h3><strong>{{ metrics.model.total }} 次</strong><p>{{ outcome(metrics.model) }}</p><p>成功率：{{ rate(metrics.model) }}</p><p>累计耗时：{{ milliseconds(metrics.model.duration_ms, metrics.model.duration_complete) }}</p></section>
              <section class="metric-card"><h3>工具调用</h3><strong>{{ metrics.tool.total }} 次</strong><p>{{ outcome(metrics.tool) }}</p><p>成功率：{{ rate(metrics.tool) }}</p><p>累计耗时：{{ milliseconds(metrics.tool.duration_ms, metrics.tool.duration_complete) }}</p></section>
              <section class="metric-card"><h3>Token 用量</h3><strong>{{ tokenValue(metrics.tokens.total_tokens) }}</strong><p>输入 {{ tokenValue(metrics.tokens.input_tokens) }} · 输出 {{ tokenValue(metrics.tokens.output_tokens) }}</p><p>采集覆盖：{{ metrics.tokens.total_calls ? `已记录 ${metrics.tokens.covered_calls}/${metrics.tokens.total_calls} 次调用` : '无模型调用' }}</p><p v-if="metrics.tokens.total_calls && !metrics.tokens.complete" class="warning">已知用量，用量不完整；未采集的用量不按零计算。</p></section>
            </div>
            <p v-if="metrics.task.observation_incomplete || uncertain(metrics.model) || uncertain(metrics.tool)" class="warning">成功率仅基于已记录且结果明确的调用；中断和状态未确定的调用未计入分母。</p>
            <h3>模型明细</h3>
            <div class="table-scroll"><table><thead><tr><th>角色</th><th>调用</th><th>成功 / 失败 / 中断 / 未确定</th><th>成功率</th><th>累计耗时</th><th>Token（输入 / 输出 / 总量）</th></tr></thead>
              <tbody><tr v-for="item in metrics.model_details" :key="item.name"><th scope="row">{{ item.name }}</th><td>{{ item.total }}</td><td>{{ counts(item) }}</td><td>{{ rate(item) }}</td><td>{{ milliseconds(item.duration_ms, item.duration_complete) }}</td><td>{{ tokenValue(item.tokens.input_tokens) }} / {{ tokenValue(item.tokens.output_tokens) }} / {{ tokenValue(item.tokens.total_tokens) }}<small v-if="!item.tokens.complete"> · 已记录 {{ item.tokens.covered_calls }}/{{ item.tokens.total_calls }}</small></td></tr>
                <tr v-if="!metrics.model_details.length"><td colspan="6" class="muted">暂无已记录的模型调用</td></tr></tbody></table></div>
            <h3>工具明细</h3>
            <div class="table-scroll"><table><thead><tr><th>工具</th><th>调用</th><th>成功 / 失败 / 中断 / 未确定</th><th>成功率</th><th>累计耗时</th></tr></thead>
              <tbody><tr v-for="item in metrics.tool_details" :key="item.name"><th scope="row">{{ item.name }}</th><td>{{ item.total }}</td><td>{{ counts(item) }}</td><td>{{ rate(item) }}</td><td>{{ milliseconds(item.duration_ms, item.duration_complete) }}</td></tr>
                <tr v-if="!metrics.tool_details.length"><td colspan="5" class="muted">暂无已记录的工具调用</td></tr></tbody></table></div>
          </template>
        </section>
        <div v-else class="trace">
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
import { listTasks, getTask, getSpans, getSpan, getTaskResult, getTaskMetrics, errorText, stepName, stateName } from '@/services/api'
import type { TaskStatus, SpanSummary, SpanDetail, TaskMetrics, CallMetrics } from '@/types'
const route = useRoute(), router = useRouter()
const states = ['accepted', 'running', 'succeeded', 'failed', 'interrupted']
const tasks = ref<TaskStatus[]>([]), selected = ref<TaskStatus | null>(null), spans = ref<SpanSummary[]>([])
const selectedId = ref(''), detail = ref<SpanDetail | null>(null), error = ref(''), filter = ref(''), offset = ref(0)
const expanded = ref<string[]>([]), tick = ref(Date.now())
const view = ref<'metrics' | 'trace'>('trace'), metrics = ref<TaskMetrics | null>(null)
const metricLoading = ref(false), metricError = ref('')
let timer: ReturnType<typeof setTimeout> | undefined, clock: ReturnType<typeof setInterval> | undefined
let disposed = false, generation = 0, detailGeneration = 0, metricGeneration = 0, viewInitialized = false
const active = (task: TaskStatus) => task.status === 'accepted' || task.status === 'running'
const dateText = (value: string | null) => value ? new Date(value).toLocaleString() : '未知'
const pretty = (value: unknown) => value == null ? '无可用内容' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const tokenValue = (value: number | null) => value === null ? '未采集' : value.toLocaleString()
const milliseconds = (value: number | null, complete = true) => value === null ? '未知' : `${(value / 1000).toFixed(1)} 秒${complete ? '' : '（已知，部分缺失）'}`
const counts = (value: CallMetrics) => `${value.succeeded} / ${value.failed} / ${value.interrupted} / ${value.unknown}`
const outcome = (value: CallMetrics) => `成功 ${value.succeeded} · 失败 ${value.failed} · 中断 ${value.interrupted} · 未确定 ${value.unknown}`
const rate = (value: CallMetrics) => value.success_rate === null ? '暂无数据' : `${(value.success_rate * 100).toFixed(1)}%`
const uncertain = (value: CallMetrics) => value.interrupted > 0 || value.unknown > 0
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
async function loadMetrics(force = false) {
  if (!selected.value || active(selected.value) || (!force && (metrics.value || metricLoading.value))) return
  const task = selectedId.value, version = ++metricGeneration
  metrics.value = null; metricError.value = ''; metricLoading.value = true
  try {
    const value = await getTaskMetrics(task)
    if (!disposed && version === metricGeneration && task === selectedId.value) metrics.value = value
  } catch (e) {
    if (!disposed && version === metricGeneration && task === selectedId.value) metricError.value = errorText(e)
  } finally {
    if (!disposed && version === metricGeneration) metricLoading.value = false
  }
}
function setView(next: 'metrics' | 'trace') {
  view.value = next; viewInitialized = true
  void router.replace({ query: { task: selectedId.value, view: next } })
  if (next === 'metrics') void loadMetrics()
}
async function refresh(reloadMetrics = false) {
  clearTimeout(timer)
  const version = ++generation
  if (reloadMetrics) { metricGeneration++; metrics.value = null; metricLoading.value = false; metricError.value = '' }
  try {
    const rows = await listTasks(filter.value, offset.value)
    if (disposed || version !== generation) return
    tasks.value = rows
    if (selectedId.value) {
      const [task, records] = await Promise.all([getTask(selectedId.value), getSpans(selectedId.value)])
      if (disposed || version !== generation) return
      selected.value = task; spans.value = records
      if (!viewInitialized) {
        view.value = active(task) ? 'trace' : 'metrics'
        viewInitialized = true
        void router.replace({ query: { task: selectedId.value, view: view.value } })
      }
      if (view.value === 'metrics' && !active(task) && !metricError.value) void loadMetrics()
      const existing = new Set(expanded.value)
      records.filter(s => s.parent_span_id === null || s.operation_type === 'agent').forEach(s => existing.add(s.span_id))
      expanded.value = [...existing]
    }
    error.value = ''
  } catch (e: any) {
    if (disposed || version !== generation) return
    error.value = `查询失败：${errorText(e)}（不代表规划失败）`
    if (e.response?.status === 404) {
      selected.value = null; selectedId.value = ''; spans.value = []; detail.value = null
      metricGeneration++; metrics.value = null; metricLoading.value = false; metricError.value = ''
    }
  }
  if (!disposed && version === generation && (error.value || tasks.value.some(active) || (selected.value && active(selected.value)))) timer = setTimeout(refresh, 2000)
}
function select(id: string) {
  selectedId.value = id; selected.value = null; detail.value = null; spans.value = []; expanded.value = []
  detailGeneration++; metricGeneration++; metrics.value = null; metricLoading.value = false; metricError.value = ''; viewInitialized = false
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
  if (route.query.view === 'metrics' || route.query.view === 'trace') {
    view.value = route.query.view
    viewInitialized = true
  }
  void refresh(); clock = setInterval(() => { tick.value = Date.now() }, 1000)
})
onUnmounted(() => { disposed = true; metricGeneration++; detailGeneration++; clearTimeout(timer); clearInterval(clock) })
</script>

<style scoped>
.observatory { max-width: 1500px; margin: auto; padding: 32px; color: #17243b; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
h1 { margin: 0; } header p, .muted { color: #65738a; }
.workspace { display: grid; grid-template-columns: 290px minmax(0, 1fr); gap: 24px; }
aside, article { background: white; border: 1px solid #dde4ed; border-radius: 12px; padding: 20px; min-width: 0; }
.task { display: flex; flex-direction: column; text-align: left; width: 100%; margin: 12px 0; padding: 12px; border: 1px solid #e1e6ef; background: #fafbfe; border-radius: 8px; cursor: pointer; gap: 5px; overflow-wrap: anywhere; }
.task.selected { border-color: #346ac5; background: #edf4ff; } small { font-weight: normal; font-size: 11px; overflow-wrap: anywhere; }
.views { display: flex; gap: 8px; margin-top: 22px; border-bottom: 1px solid #dde4ed; }
.views button { background: transparent; border: 0; border-bottom: 2px solid transparent; color: #536680; cursor: pointer; padding: 10px 16px; font: inherit; }
.views button.current { color: #2456ad; border-bottom-color: #2456ad; font-weight: 600; }
.views button:focus-visible { outline: 2px solid #2456ad; outline-offset: 2px; }
.metrics { padding-top: 20px; }
.cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 18px 0 28px; }
.metric-card { border: 1px solid #dde4ed; border-radius: 10px; padding: 16px; background: #fafcff; overflow-wrap: anywhere; }
.metric-card h3 { margin: 0 0 12px; font-size: 15px; }
.metric-card strong { font-size: 22px; }
.metric-card p { margin: 10px 0 0; }
.table-scroll { overflow-x: auto; margin: 10px 0 26px; }
table { width: 100%; border-collapse: collapse; min-width: 650px; text-align: left; }
th, td { padding: 11px 10px; border-bottom: 1px solid #e4eaf2; vertical-align: top; }
thead { background: #f4f7fb; }
th { font-weight: 600; }
.trace { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(0, 1.3fr); gap: 20px; margin-top: 24px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 450px; overflow: auto; padding: 14px; background: #f4f6fa; border-radius: 8px; font-size: 12px; }
.warning { color: #946000; }
@media(max-width: 900px) { .workspace, .trace { grid-template-columns: 1fr; } .observatory { padding: 16px; } }
@media(max-width: 600px) { .cards { grid-template-columns: 1fr; } header { align-items: flex-start; gap: 12px; } }
</style>
