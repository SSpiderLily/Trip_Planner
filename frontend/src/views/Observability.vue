<template>
  <main class="observatory">
    <header class="page-header"><div><span class="eyebrow">AGENT OBSERVABILITY</span><h1>规划观测</h1><p>从执行过程定位耗时、用量与异常</p></div><router-link to="/">返回旅行规划 ↗</router-link></header>
    <section class="workspace">
      <aside class="task-sidebar">
        <button class="mobile-tasks" :aria-expanded="tasksOpen" @click="tasksOpen = !tasksOpen">任务记录 {{ tasksOpen ? '▴' : '▾' }}</button>
        <div class="task-content" :class="{ open: tasksOpen }"><div class="sidebar-heading"><h2>任务记录</h2><button @click="refreshAll">刷新</button></div>
          <select v-model="filter" aria-label="任务状态筛选" @change="resetList"><option value="">全部状态</option><option v-for="state in states" :key="state" :value="state">{{ stateName(state) }}</option></select>
          <div v-if="listError" class="query-error">任务列表查询失败：{{ listError }}<button @click="loadList">重试</button></div><p v-if="listLoading && !tasks.length" class="muted">正在读取任务…</p><p v-else-if="!tasks.length && !listError" class="muted">暂无任务记录</p>
          <div class="task-list"><button v-for="task in tasks" :key="task.task_id" class="task" :class="{ selected: selectedId === task.task_id }" :aria-pressed="selectedId === task.task_id" @click="selectTask(task.task_id)"><span><i :class="task.status"></i><strong>{{ stateName(task.status) }}</strong></span><time>{{ dateText(task.created_at) }}</time><small>{{ task.task_id }}</small><em v-if="task.observation_incomplete">记录不完整</em></button></div>
          <div class="pagination"><button :disabled="offset === 0" @click="page(-50)">上一页</button><span>{{ offset + 1 }}—{{ offset + tasks.length }}</span><button :disabled="tasks.length < 50" @click="page(50)">下一页</button></div>
        </div>
      </aside>
      <article v-if="selected" class="task-workspace">
        <div class="task-heading"><div><div class="title-line"><span class="status" :class="selected.status">{{ stateName(selected.status) }}</span><span class="record-state">{{ selected.observation_incomplete ? '记录不完整' : '未发现记录缺失' }}</span></div><h2>单次规划执行分析</h2><p>{{ selected.task_id }} · {{ dateText(selected.created_at) }}</p></div><button v-if="selected.status === 'succeeded'" class="result-button" @click="showResult">查看行程结果 ↗</button></div>
        <div class="alerts">
          <div v-if="taskError" class="query-error">任务状态查询失败：{{ taskError }}（不代表规划失败）<button @click="loadTask()">重试</button></div>
          <p v-if="selected.current_step && active">{{ stepName(selected.current_step.name) }} · 已等待 {{ milliseconds(Math.max(0, tick - (time(selected.current_step.started_at) ?? tick))) }}</p>
          <p v-if="selected.observation_incomplete" class="warning">观测记录不完整，缺少步骤不代表没有执行。Token覆盖仅针对已记录的调用。</p>
          <p v-if="selected.persistence_error" class="danger">{{ selected.persistence_error.message }}；终态尚未持久化。</p>
          <p v-if="selected.error_code" class="danger">{{ selected.error_code }} · {{ selected.error_message }} <span v-if="selected.error_step">（{{ stepName(selected.error_step) }}）</span></p>
          <p v-if="selected.status === 'interrupted'">发现中断：{{ dateText(selected.interruption_detected_at) }}；实际结束时间未知。</p>
          <p v-if="resultError" class="danger">结果查询失败：{{ resultError }}</p>
        </div>
        <div class="summary-cards">
          <section><span>任务总耗时</span><strong>{{ active ? elapsed : milliseconds(metrics?.task.duration_ms ?? durationMs({ started_at: selected.created_at, finished_at: selected.finished_at })) }}</strong><small>{{ active ? '执行至今，尚未结束' : '创建至实际结束，不累加子步骤' }}</small></section>
          <section><span>外部调用</span><strong>{{ active ? '执行中' : metrics ? metrics.model.total + metrics.tool.total + ' 次' : '待读取' }}</strong><small>{{ metrics ? `模型 ${metrics.model.total} 次 · 工具 ${metrics.tool.total} 次` : active ? '调用记录每2秒更新' : '仅统计已记录调用' }}</small></section>
          <section><span>Token 总用量</span><strong>{{ active ? '待任务结束' : metrics ? tokenText(metrics.tokens.total_tokens) : '待读取' }}</strong><small v-if="metrics">{{ metrics.tokens.total_calls ? `已记录 ${metrics.tokens.covered_calls} 次，共 ${metrics.tokens.total_calls} 次模型调用` : '无模型调用' }}{{ metrics.tokens.complete ? '' : ' · 用量不完整' }}</small><small v-else>输入与输出缺失不按零计算</small></section>
          <section><span>异常调用 <small v-if="active">（已记录）</small></span><strong :class="{ danger: anomalies.total }">{{ spanError ? '暂不可用' : spanLoading && !spans.length ? '待读取' : anomalies.total }} <em v-if="!spanError && !(spanLoading && !spans.length)">次</em></strong><small v-if="!spanError && !(spanLoading && !spans.length)">调用失败 {{ anomalies.failed }} · 中断未完成 {{ anomalies.interrupted }}</small><small v-else>等待调用摘要，不将缺失记录当作零</small></section>
        </div>
        <div v-if="metricError" class="query-error">指标查询失败：{{ metricError }}（不代表规划失败）<button @click="loadMetrics(true)">重试</button></div><p v-else-if="metricLoading" class="loading-note">正在读取工程指标…</p>
        <nav class="views" aria-label="观测视图"><button v-for="tab in tabs" :key="tab.key" :class="{ current: view === tab.key }" :aria-current="view === tab.key ? 'page' : undefined" @click="setView(tab.key)">{{ tab.label }}<span v-if="tab.key === 'errors' && anomalies.total">{{ anomalies.total }}</span></button></nav>
        <div class="analysis-workspace">
          <div class="main-panel">
            <div v-if="spanError" class="query-error">调用摘要查询失败：{{ spanError }}（不代表规划失败）<button @click="loadTask()">重试</button></div>
            <p v-if="spanLoading && !spans.length" class="loading-note">正在读取调用记录…</p>
            <div v-if="spanError && !spans.length" class="analysis-empty">调用摘要暂不可用，请重试。</div>
            <ObservationTimeline v-else-if="view === 'trace'" :key="selectedId" :task="selected" :spans="spans" :selected-id="selectedSpanId" :now="tick" @select="inspect" />
            <div v-else-if="active" class="analysis-empty">任务结束后可分析<p>执行轨迹每2秒更新，可先查看正在进行的调用。</p><button @click="setView('trace')">查看执行轨迹</button></div>
            <ObservationAnalysis v-else-if="metrics" :view="view" :spans="spans" :metrics="metrics" :selected-id="selectedSpanId" @select="inspect" @locate="locate" />
            <div v-else class="analysis-empty">{{ metricLoading ? '正在读取工程指标…' : '工程指标暂不可用，请重试。' }}</div>
          </div>
          <div ref="detailsElement" class="detail-panel"><ObservationDetails :detail="detail" :span-id="selectedSpanId" :loading="detailLoading" :error="detailError" :active="active" :now="tick" @retry="loadDetail(selectedSpanId)" /></div>
        </div>
        <footer class="page-note">工程指标用于分析执行过程，不代表行程业务质量。所有视图来自已保存的调用记录。</footer>
      </article>
      <article v-else class="unselected"><div v-if="taskError" class="query-error">任务查询失败：{{ taskError }}<button v-if="selectedId" @click="loadTask()">重试</button></div><p>{{ taskLoading ? '正在读取任务…' : '选择一个任务查看执行过程' }}</p></article>
    </section>
  </main>
</template>
<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ObservationTimeline from '@/components/ObservationTimeline.vue'
import ObservationDetails from '@/components/ObservationDetails.vue'
import ObservationAnalysis from '@/components/ObservationAnalysis.vue'
import { listTasks, getTask, getSpans, getSpan, getTaskResult, getTaskMetrics, errorText, stepName, stateName } from '@/services/api'
import { anomalyCounts, defaultSelection, durationMs, isActive, milliseconds, normalizeSpan, time, tokenText } from '@/services/observationView'
import type { TaskStatus, SpanSummary, SpanDetail, TaskMetrics } from '@/types'
type View = 'trace' | 'tokens' | 'tools' | 'errors'
const tabs: { key: View; label: string }[] = [{ key: 'trace', label: '执行轨迹' }, { key: 'tokens', label: 'Token 分析' }, { key: 'tools', label: '工具分析' }, { key: 'errors', label: '异常调用' }]
const states = ['accepted', 'running', 'succeeded', 'failed', 'interrupted']
const route = useRoute(), router = useRouter()
const tasks = ref<TaskStatus[]>([]), selected = ref<TaskStatus | null>(null), selectedId = ref(''), spans = ref<SpanSummary[]>([])
const view = ref<View>('trace'), selectedSpanId = ref(''), detail = ref<SpanDetail | null>(null), metrics = ref<TaskMetrics | null>(null)
const filter = ref(''), offset = ref(0), tasksOpen = ref(false), tick = ref(Date.now()), detailsElement = ref<HTMLElement | null>(null)
const listLoading = ref(false), taskLoading = ref(false), spanLoading = ref(false), metricLoading = ref(false), detailLoading = ref(false)
const listError = ref(''), taskError = ref(''), spanError = ref(''), metricError = ref(''), detailError = ref(''), resultError = ref('')
let disposed = false, taskVersion = 0, listVersion = 0, metricVersion = 0, detailVersion = 0, autoSelected = false
let taskTimer: ReturnType<typeof setTimeout> | undefined, listTimer: ReturnType<typeof setTimeout> | undefined, clock: ReturnType<typeof setInterval> | undefined
const active = computed(() => !!selected.value && isActive(selected.value))
const anomalies = computed(() => anomalyCounts(spans.value))
const elapsed = computed(() => milliseconds(Math.max(0, tick.value - (time(selected.value?.created_at) ?? tick.value))))
const dateText = (value: string | null) => time(value) === null ? '未知' : new Date(value!).toLocaleString()
function clearTask(id: string) {
  clearTimeout(taskTimer); taskVersion++; metricVersion++; detailVersion++; autoSelected = false
  selectedId.value = id; selected.value = null; spans.value = []; selectedSpanId.value = ''; detail.value = null; metrics.value = null
  taskError.value = ''; spanError.value = ''; metricError.value = ''; detailError.value = ''; resultError.value = ''
  taskLoading.value = false; spanLoading.value = false; metricLoading.value = false; detailLoading.value = false
}
async function loadList() {
  clearTimeout(listTimer)
  const version = ++listVersion; listLoading.value = true; listError.value = ''
  try { const rows = await listTasks(filter.value, offset.value); if (disposed || version !== listVersion) return; tasks.value = rows }
  catch (e) { if (!disposed && version === listVersion) listError.value = errorText(e) }
  finally { if (!disposed && version === listVersion) { listLoading.value = false; if (tasks.value.some(isActive)) listTimer = setTimeout(loadList, 2000) } }
}
async function loadTask(forceMetrics = false) {
  clearTimeout(taskTimer)
  const id = selectedId.value, version = ++taskVersion
  if (!id) return
  taskLoading.value = true; spanLoading.value = true
  const [taskResult, spanResult] = await Promise.allSettled([getTask(id), getSpans(id)])
  if (disposed || version !== taskVersion || id !== selectedId.value) return
  taskLoading.value = false; spanLoading.value = false
  if (taskResult.status === 'rejected') {
    taskError.value = errorText(taskResult.reason)
    if (taskResult.reason.response?.status === 404) { clearTask(''); taskError.value = '任务不存在或已清理'; void router.replace({ query: { ...route.query, task: undefined, view: view.value } }); return }
  } else { selected.value = taskResult.value; taskError.value = '' }
  if (spanResult.status === 'rejected') spanError.value = errorText(spanResult.reason)
  else {
    spans.value = spanResult.value.map(normalizeSpan); spanError.value = ''
    if (!autoSelected && spans.value.length) { autoSelected = true; const id = defaultSelection(spans.value); if (id) inspect(id, false) }
  }
  if (selected.value && !isActive(selected.value) && (!metricError.value || forceMetrics)) void loadMetrics(forceMetrics)
  if (taskError.value || active.value) taskTimer = setTimeout(() => loadTask(), 2000)
}
async function loadMetrics(force = false) {
  if (!selected.value || isActive(selected.value) || (!force && (metrics.value || metricLoading.value))) return
  const id = selectedId.value, version = ++metricVersion
  metricLoading.value = true; metricError.value = ''; if (force) metrics.value = null
  try { const value = await getTaskMetrics(id); if (!disposed && version === metricVersion && id === selectedId.value) metrics.value = value }
  catch (e) { if (!disposed && version === metricVersion && id === selectedId.value) metricError.value = errorText(e) }
  finally { if (!disposed && version === metricVersion) metricLoading.value = false }
}
async function loadDetail(id: string) {
  if (!id || !selectedId.value) return
  const task = selectedId.value, version = ++detailVersion
  selectedSpanId.value = id; detail.value = null; detailError.value = ''; detailLoading.value = true
  try { const value = await getSpan(task, id); if (!disposed && version === detailVersion && task === selectedId.value && id === selectedSpanId.value) detail.value = normalizeSpan(value) }
  catch (e) { if (!disposed && version === detailVersion && task === selectedId.value) detailError.value = errorText(e) }
  finally { if (!disposed && version === detailVersion) detailLoading.value = false }
}
function inspect(id: string, scroll = true) {
  if (selectedSpanId.value !== id || (!detail.value && !detailLoading.value)) void loadDetail(id)
  if (scroll) void nextTick(() => { if (window.matchMedia('(max-width: 1000px)').matches) detailsElement.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }) })
}
function locate(id: string) { setView('trace'); inspect(id) }
function setView(next: View) { void router.push({ query: { ...route.query, task: selectedId.value, view: next } }) }
function selectTask(id: string) { tasksOpen.value = false; void router.push({ query: { ...route.query, task: id, view: view.value } }) }
function refreshAll() { void loadList(); void loadTask(true) }
function resetList() { offset.value = 0; void loadList() }
function page(delta: number) { offset.value = Math.max(0, offset.value + delta); void loadList() }
async function showResult() {
  const id = selectedId.value, version = taskVersion
  resultError.value = ''
  try { const response = await getTaskResult(id); if (disposed || id !== selectedId.value || version !== taskVersion) return; sessionStorage.setItem('tripPlan', JSON.stringify(response.data)); sessionStorage.setItem('tripTaskId', id); await router.push('/result') }
  catch (e) { if (!disposed && id === selectedId.value && version === taskVersion) resultError.value = errorText(e) }
}
watch(() => [route.query.task, route.query.view], () => {
  const id = typeof route.query.task === 'string' ? route.query.task : ''
  const next = tabs.some(tab => tab.key === route.query.view) ? route.query.view as View : 'trace'
  view.value = next
  if (route.query.view !== next) void router.replace({ query: { ...route.query, view: next } })
  if (id !== selectedId.value) { clearTask(id); if (id) void loadTask() }
}, { immediate: true })
onMounted(() => { void loadList(); clock = setInterval(() => { tick.value = Date.now() }, 1000) })
onUnmounted(() => { disposed = true; taskVersion++; listVersion++; metricVersion++; detailVersion++; clearTimeout(taskTimer); clearTimeout(listTimer); clearInterval(clock) })
</script>
<style scoped>
.observatory{max-width:1840px;margin:auto;padding:25px 30px 35px;color:#24354b;background:#f5f7f9;min-height:100vh;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.page-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:23px}.eyebrow{color:#84919e;font-size:9px;letter-spacing:1.8px}.page-header h1{font-size:23px;margin:5px 0 4px;font-weight:650}.page-header p{font-size:12px;color:#8591a1;margin:0}.page-header a{font-size:12px;color:#65788b}.workspace{display:grid;grid-template-columns:205px minmax(0,1fr);gap:20px;align-items:start}.task-sidebar,.task-workspace,.unselected{background:white;border:1px solid #e1e7ec;border-radius:10px;min-width:0;overflow:hidden}.task-content{padding:16px 12px}.sidebar-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.sidebar-heading h2{font-size:13px;margin:0}.sidebar-heading button,.pagination button{border:1px solid #dfe5eb;border-radius:5px;background:white;padding:4px 8px;font-size:10px;color:#6c7b8f;cursor:pointer}.task-content>select{width:100%;background:#f8fafb;border:1px solid #e4e9ee;padding:7px 9px;border-radius:6px;font-size:11px;color:#6d7a8c}.task-list{max-height:690px;overflow:auto;margin-top:13px}.task{width:100%;background:white;border:1px solid transparent;border-radius:6px;padding:12px 9px;text-align:left;cursor:pointer;margin-bottom:7px;display:flex;flex-direction:column;gap:6px;overflow-wrap:anywhere}.task:hover{background:#f7faf9}.task.selected{border-color:#cae4d8;background:#eff7f3}.task>span{display:flex;align-items:center;gap:7px}.task strong{font-size:11px;font-weight:600}.task i{width:6px;height:6px;background:#9aa7b4;border-radius:50%}.task i.succeeded{background:#319977}.task i.failed,.task i.interrupted{background:#d07a70}.task i.running{background:#dba34e}.task time{font-size:10px;color:#8793a2}.task small{font-size:9px;color:#9ca5b3}.task em{font-size:9px;color:#ad8850;font-style:normal}.pagination{display:flex;justify-content:space-between;align-items:center;gap:3px;padding-top:13px}.pagination span{font-size:9px;color:#99a4b0}.pagination button:disabled{opacity:.4;cursor:default}.task-heading{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:24px 25px 8px}.title-line{display:flex;gap:10px;align-items:center}.status{font-size:10px;border-radius:4px;padding:3px 7px;background:#f0f3f7;color:#6b788a}.status.succeeded{color:#258566;background:#eaf5ef}.status.failed,.status.interrupted{color:#bf6d61;background:#fdf0ed}.status.running{color:#a9813e;background:#fff5df}.record-state{font-size:10px;color:#8a96a4}.task-heading h2{font-size:19px;margin:12px 0 7px;font-weight:600}.task-heading p{font-size:10px;color:#8a96a4;margin:0;overflow-wrap:anywhere}.result-button{border:1px solid #d6e2db;background:white;border-radius:6px;padding:8px 10px;font-size:10px;white-space:nowrap;color:#44816b;cursor:pointer}.alerts{padding:0 25px;font-size:11px;color:#8995a4;overflow-wrap:anywhere}.alerts p{margin:8px 0}.warning{color:#ab8045}.danger{color:#c76f65}.summary-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;padding:18px 25px 24px}.summary-cards section{border:1px solid #e6ebef;border-radius:7px;padding:14px 15px;min-width:0}.summary-cards section>span{display:block;font-size:10px;color:#8290a0}.summary-cards strong{display:block;font-size:24px;font-weight:600;letter-spacing:-.6px;margin:9px 0 6px;overflow-wrap:anywhere}.summary-cards small{font-size:9px;color:#939eac;display:block;line-height:1.8}.summary-cards span small{display:inline}.summary-cards em{font-size:12px;font-weight:400;font-style:normal}.views{display:flex;padding:0 25px;border-top:1px solid #eef1f4;border-bottom:1px solid #e6ebef;gap:25px;overflow-x:auto}.views>button{padding:14px 0;border:0;border-bottom:2px solid transparent;background:transparent;font-size:12px;color:#8692a1;white-space:nowrap;cursor:pointer}.views>button.current{border-bottom-color:#278f70;color:#278469;font-weight:600}.views>button span{background:#fff0ed;color:#c56f65;font-size:9px;padding:2px 5px;border-radius:4px;margin-left:5px}.analysis-workspace{display:grid;grid-template-columns:minmax(0,1fr) 300px;min-height:510px}.main-panel{min-width:0;overflow:hidden}.detail-panel{border-left:1px solid #e5ebef;min-width:0;max-height:810px;overflow:auto;scroll-margin-top:20px}.page-note{padding:13px 20px;border-top:1px solid #e7edf1;font-size:10px;color:#97a2b0}.query-error{font-size:11px;background:#fff4ed;color:#b47253;padding:12px 15px;margin:10px;border-radius:5px;overflow-wrap:anywhere}.query-error button,.analysis-empty button{display:inline-block;border:1px solid #e3d6c9;background:white;border-radius:5px;padding:4px 9px;font-size:10px;color:#9e7254;cursor:pointer;margin:5px}.analysis-empty{text-align:center;padding:100px 20px;font-size:14px;color:#8290a0}.analysis-empty p{font-size:12px}.loading-note{font-size:11px;color:#8796a7;padding:10px 20px;margin:0}.unselected{padding:60px 20px;text-align:center;color:#8995a4}.mobile-tasks{display:none}.muted{font-size:11px;color:#97a3b2;padding:8px}button:focus-visible,select:focus-visible{outline:2px solid #21866b;outline-offset:2px}
@media(min-width:1600px){.analysis-workspace{grid-template-columns:minmax(0,1fr) 340px}.workspace{grid-template-columns:230px minmax(0,1fr)}}
@media(max-width:1200px){.summary-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.observatory{padding:20px}.workspace{grid-template-columns:185px minmax(0,1fr);gap:15px}.analysis-workspace{grid-template-columns:minmax(0,1fr) 270px}}
@media(max-width:1000px){.analysis-workspace{grid-template-columns:minmax(0,1fr)}.detail-panel{border-left:0;border-top:1px solid #e5ebef;max-height:none}.workspace{grid-template-columns:175px minmax(0,1fr)}}
@media(max-width:700px){.observatory{padding:16px 12px}.workspace{grid-template-columns:minmax(0,1fr);gap:12px}.page-header{align-items:flex-start;gap:12px}.page-header a{font-size:10px;white-space:nowrap;margin-top:10px}.page-header h1{font-size:21px}.mobile-tasks{display:block;width:100%;border:0;background:white;padding:12px 15px;font-size:12px;text-align:left;color:#65788b;cursor:pointer}.task-content{display:none}.task-content.open{display:block}.task-list{max-height:300px}.task-heading{padding:19px 16px 8px;align-items:flex-start;flex-wrap:wrap}.task-heading h2{font-size:17px}.alerts{padding:0 16px}.summary-cards{padding:12px 16px 18px;gap:8px}.summary-cards section{padding:11px}.summary-cards strong{font-size:20px}.views{padding:0 16px;gap:23px}.page-note{line-height:1.8}.analysis-workspace{min-height:0}}
</style>
