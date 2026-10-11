<template>
  <main class="preview-shell">
    <section v-if="pageState === 'loading'" class="state-page" aria-live="polite">
      <span class="eyebrow">TRIP PREVIEW</span>
      <h1>正在准备你的行程</h1>
      <p>{{ progressLabel }}。完成后会自动显示；重新打开此页也会继续读取当前预览。</p>
      <div class="loading-line"><i></i><i></i><i></i></div>
    </section>

    <section v-else-if="pageState === 'missing_access'" class="state-page">
      <span class="eyebrow">TRIP PREVIEW</span>
      <h1>这里还没有行程预览</h1>
      <p>从首页填写旅行计划后，预览会保存在这台设备上，方便你稍后继续查看。</p>
      <router-link class="quiet-link" to="/">返回首页</router-link>
    </section>

    <section v-else-if="pageState === 'failed'" class="state-page">
      <span class="eyebrow">TRIP PREVIEW</span>
      <h1>暂时无法打开这份行程</h1>
      <p>{{ errorMessage }}</p>
      <div class="state-actions"><button class="quiet-button" @click="loadLatest">重试</button><router-link class="quiet-link" to="/">返回首页</router-link></div>
    </section>

    <template v-else-if="view && activeDay">
      <header class="page-topline">
        <span class="eyebrow">YOUR TRIP</span>
        <span class="readonly-label">行程预览</span>
      </header>
      <section class="hero">
        <div>
          <p class="hero-kicker">A JOURNEY MADE FOR YOU</p>
          <h1>{{ view.city }} · 行程预览</h1>
          <p class="date-range">{{ formatDate(view.arrivalAt, view.timezone) }} — {{ formatDate(view.departureAt, view.timezone) }}</p>
        </div>
        <div class="cost-summary" aria-label="已知人均消费参考">
          <span>已知人均景点与餐饮消费参考</span>
          <strong v-if="view.cost.knownSubtotal !== null">¥{{ view.cost.knownSubtotal }}</strong>
          <strong v-else>暂无可用金额</strong>
          <small v-if="view.tripBudget !== null">全程旅费预算 ¥{{ view.tripBudget }}</small>
          <small v-if="view.cost.unknownCount || view.cost.conflictCount">
            {{ costGapText(view.cost.unknownCount, view.cost.conflictCount) }}
          </small>
          <small>不含交通；住宿消费单独展示。已知小计不代表完整旅费。</small>
        </div>
      </section>

      <section v-if="view.planIssues.length" class="global-issues" aria-label="行程提示">
        <div v-for="issue in view.planIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
          <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
        </div>
      </section>

      <nav class="day-tabs" aria-label="行程日期">
        <button
          v-for="(day, index) in view.days"
          :key="day.id"
          type="button"
          :class="{ active: activeIndex === index }"
          :aria-current="activeIndex === index ? 'date' : undefined"
          @click="activeIndex = index"
        >
          <span>第 {{ index + 1 }} 天</span>
          <small>{{ formatShortDate(day.date) }}</small>
          <i>{{ day.activities.filter((activity) => activity.kind === 'sightseeing').length }} 个景点 · {{ day.activities.filter((activity) => activity.kind === 'meal').length }} 餐</i>
        </button>
      </nav>

      <section class="day-overview">
        <div class="overview-copy">
          <span class="eyebrow">DAY {{ activeIndex + 1 }} / {{ activeDay.date }}</span>
          <h2>{{ activeDay.theme }}</h2>
          <p>{{ activityCountText(activeDay.activities.length) }}</p>
        </div>
        <div class="daily-facts">
          <span>已知停留 {{ formatReferenceDuration(activeDay.timing.knownStaySeconds) }}</span>
          <span>已知交通 {{ formatReferenceDuration(activeDay.timing.knownTravelSeconds) }}</span>
          <span v-if="activeDay.timing.unknownLegCount > 0">{{ activeDay.timing.unknownLegCount }} 段交通耗时待确认</span>
          <span v-else-if="activeDay.timing.elapsedSeconds !== null">参考安排 {{ formatReferenceDuration(activeDay.timing.elapsedSeconds) }}</span>
          <span v-if="activeDay.cost.knownSubtotal !== null">当日已知景点与餐饮 ¥{{ activeDay.cost.knownSubtotal }} / 人</span>
          <span v-if="activeDay.cost.unknownCount || activeDay.cost.conflictCount">{{ costGapText(activeDay.cost.unknownCount, activeDay.cost.conflictCount) }}</span>
        </div>
      </section>

      <div class="day-layout">
        <section class="timeline-column" aria-label="当天有序行程">
          <section class="info-card weather-card">
            <div class="card-topline"><span class="eyebrow">WEATHER</span><span :class="activeDay.weather.known ? 'fact-known' : 'fact-unknown'">{{ activeDay.weather.known ? '旅行日预报' : '天气未知' }}</span></div>
            <h3>{{ activeDay.weather.summary }}</h3>
            <p>{{ activeDay.weather.advice }}</p>
          </section>

          <section class="info-card lodging-card">
            <div class="card-topline"><span class="eyebrow">STAY</span><span class="fact-known">住宿参考</span></div>
            <div class="lodging-grid">
              <div>
                <small>{{ activeDay.hotel.label }}</small>
                <h3>{{ activeDay.hotel.name }}</h3>
                <p v-if="activeDay.hotel.address">{{ activeDay.hotel.address }}</p>
                <p v-else class="muted">具体酒店未核实时，会保留未知状态。</p>
                <small class="hotel-cost">住宿人均消费参考：{{ activeDay.hotel.cost.text }} · 不按房晚推算</small>
                <small v-if="activeDay.hotel.cost.evidenceLabel" class="cost-evidence">{{ activeDay.hotel.cost.evidenceLabel }}</small>
              </div>
              <div class="area-reference">
                <small>推荐寻找区域</small>
                <h3>{{ activeDay.area.label }}</h3>
                <p>住宿区域与具体酒店分别展示。区域参考点不代表已选酒店。</p>
              </div>
            </div>
          </section>

          <section v-if="activeDay.returnTiming.status !== 'not_applicable'" class="return-card" :class="activeDay.returnTiming.status" aria-label="离开日返程核验">
            <div class="card-topline"><span class="eyebrow">RETURN</span><span>{{ activeDay.returnTiming.status === 'verified' ? '已核实' : activeDay.returnTiming.status === 'conflict' ? '时间冲突' : '待核实' }}</span></div>
            <h3>{{ activeDay.returnTiming.headline }}</h3>
            <div class="return-facts">
              <span>{{ activeDay.returnTiming.bufferLabel }}</span>
              <span v-if="activeDay.returnTiming.departureLabel">{{ activeDay.returnTiming.departureLabel }}</span>
              <span v-if="activeDay.returnTiming.deadlineLabel">{{ activeDay.returnTiming.deadlineLabel }}</span>
              <span v-if="activeDay.returnTiming.overrunLabel">{{ activeDay.returnTiming.overrunLabel }}</span>
            </div>
            <p v-if="activeDay.returnTiming.detail">{{ activeDay.returnTiming.detail }}</p>
            <div v-if="activeDay.returnLeg" class="return-leg-route">
              <div class="leg-heading"><span>{{ activeDay.returnLeg.fromLabel }} → {{ activeDay.returnLeg.toLabel }}</span><b>{{ activeDay.returnLeg.selectedModeLabel }} · {{ activeDay.returnLeg.selectedDurationLabel }}</b></div>
              <div v-if="activeDay.returnLeg.modes.length" class="mode-list" aria-label="返程交通方式耗时参考">
                <div v-for="mode in activeDay.returnLeg.modes" :key="mode.mode" class="mode-chip" :class="{ chosen: activeDay.returnLeg.selectedMode === mode.mode }"><span>{{ mode.label }}</span><b>{{ mode.durationLabel }}</b></div>
              </div>
              <p class="route-note">{{ activeDay.returnLeg.geometryMessage }}</p>
              <p v-if="activeDay.returnLeg.timeBasisLabel" class="route-note">{{ activeDay.returnLeg.timeBasisLabel }}</p>
              <div v-for="issue in activeDay.returnLeg.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
            </div>
          </section>

          <div v-if="dayLevelIssues.length" class="day-issues" aria-label="当天提示">
            <div v-for="issue in dayLevelIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
              <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
            </div>
          </div>
          <div v-if="activeDay.unboundIssues.length" class="day-issues" aria-label="其他当天提示">
            <div v-for="issue in activeDay.unboundIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
              <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
            </div>
          </div>

          <section v-if="!activeDay.timeline.length" class="empty-day">
            <h3>这一天还没有具体活动</h3>
            <p>当前预览保留了这一天。安排缺项会在对应时段显示。</p>
          </section>

          <template v-for="item in activeDay.timeline" :key="item.type === 'activity' ? item.activity.id : item.type === 'missing_slot' ? item.slot.id : item.issue.issue_id">
            <template v-if="item.type === 'missing_slot'">
              <section class="missing-slot" :aria-label="`${item.slot.periodLabel}安排缺项`">
                <span class="missing-icon" :class="{ conditional: item.slot.kind === 'conditional' }">{{ item.slot.kind === 'conditional' ? '?' : '!' }}</span>
                <div><b>{{ item.slot.periodLabel }} · {{ item.slot.kind === 'conditional' ? '待确认' : '尚未安排' }}</b><p>{{ item.slot.prompt }}</p>
                  <p v-for="issue in item.slot.issues" :key="issue.issue_id" class="issue-detail">{{ issue.message }}</p>
                </div>
              </section>
            </template>
            <section v-else-if="item.type === 'slot_issue'" class="issue-row" :class="issueTone(item.issue)">
              <b>{{ item.periodLabel }} · {{ issueTitle(item.issue) }}</b><span>{{ item.issue.message }}</span>
            </section>
            <template v-else>
              <section v-if="item.incomingLeg" class="leg-card">
                <div class="leg-heading">
                  <span>{{ item.incomingLeg.fromLabel }} → {{ item.incomingLeg.toLabel }}</span>
                  <b>{{ item.incomingLeg.selectedModeLabel }} · {{ item.incomingLeg.selectedDurationLabel }}</b>
                </div>
                <div v-if="item.incomingLeg.modes.length" class="mode-list" aria-label="交通方式耗时参考">
                  <div v-for="mode in item.incomingLeg.modes" :key="mode.mode" class="mode-chip" :class="{ chosen: item.incomingLeg.selectedMode === mode.mode }">
                    <span>{{ mode.label }}</span><b>{{ mode.durationLabel }}</b>
                  </div>
                </div>
                <p class="route-note">{{ item.incomingLeg.geometryMessage }}</p>
                <p v-if="item.incomingLeg.timeBasisLabel" class="route-note">{{ item.incomingLeg.timeBasisLabel }}</p>
                <div v-for="issue in item.incomingLeg.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
              </section>
              <article class="activity-card" :class="{ meal: item.activity.kind === 'meal', free: item.activity.kind === 'free_time' }">
                <div class="activity-meta"><span>{{ item.activity.periodLabel }} · {{ activityKind(item.activity.kind) }}</span><span>{{ item.activity.durationLabel }}</span></div>
                <div class="activity-title"><span class="activity-number">{{ item.position }}</span><div><h3>{{ item.activity.title }}</h3><small>{{ item.activity.timingLabel }}</small></div></div>
                <p v-if="item.activity.description" class="activity-description">{{ item.activity.description }}</p>
                <p v-if="item.activity.address" class="activity-address">⌖ {{ item.activity.address }}</p>
                <div v-for="issue in item.activity.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
                <footer>
                  <span>{{ item.activity.cost.text }}</span>
                  <span v-if="item.activity.kind === 'free_time'">不指定地点</span>
                </footer>
                <small v-if="item.activity.cost.evidenceLabel" class="cost-evidence">{{ item.activity.cost.evidenceLabel }}</small>
              </article>
            </template>
          </template>
        </section>

        <aside class="map-column">
          <PreviewMap :locations="activeDay.locations" :routes="activeDay.legs" />
          <section class="destination-card">
            <div class="section-heading"><div><span class="eyebrow">PLACES</span><h3>当天目的地</h3></div><small>{{ activeDay.locations.filter((location) => location.kind === 'activity').length }} 个地点</small></div>
            <ul v-if="activeDay.locations.some((location) => location.kind === 'activity')" class="destination-list">
              <li v-for="location in activeDay.locations.filter((item) => item.kind === 'activity')" :key="location.id">
                <span class="destination-number">{{ location.number }}</span>
                <span><b>{{ location.name }}</b><small>{{ location.area || location.address || '区域信息未提供' }}</small></span>
                <small v-if="!location.coordinate" class="coord-missing">位置未核实</small>
              </li>
            </ul>
            <p v-else class="muted">当天没有可定位的具体目的地。</p>
          </section>

          <section class="destination-card recommendations">
            <div class="section-heading"><div><span class="eyebrow">MORE PLACES</span><h3>可选候选</h3></div><small>景点 {{ activeDay.recommendations.filter((item) => item.kind === 'sightseeing').length }} · 餐馆 {{ activeDay.recommendations.filter((item) => item.kind === 'meal').length }}</small></div>
            <p class="muted">已排除当天已安排地点；最多展示两个景点和一家餐馆。</p>
            <ul v-if="activeDay.recommendations.length" class="candidate-list">
              <li v-for="candidate in activeDay.recommendations" :key="candidate.id">
                <div><span class="candidate-type">{{ candidate.kind === 'meal' ? '餐馆' : '景点' }}</span><b>{{ candidate.name }}</b></div>
                <small>{{ candidate.area || candidate.address || '区域信息未提供' }}</small>
                <small>{{ candidate.cost.text }}</small>
                <small v-if="candidate.cost.evidenceLabel" class="cost-evidence">{{ candidate.cost.evidenceLabel }}</small>
                <p v-if="candidate.reason">{{ candidate.reason }}</p>
              </li>
            </ul>
            <p v-else class="muted">当前没有可展示的候选地点。</p>
          </section>
        </aside>
      </div>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PreviewMap from '@/components/preview/PreviewMap.vue'
import {
  getLatestPreviewAccess,
  getV4Preview,
  getV4TaskStatus,
  v4PreviewErrorMessage,
} from '@/services/v4Preview'
import type { V4PreviewAccess, V4PreviewEnvelope } from '@/types/v4Preview'
import { formatReferenceDuration, mapV4Preview, type TripPreviewView } from '@/utils/v4PreviewMapper'

const pageState = ref<'loading' | 'missing_access' | 'failed' | 'ready'>('loading')
const errorMessage = ref('')
const progressLabel = ref('正在整理旅行需求')
const view = ref<TripPreviewView | null>(null)
const activeIndex = ref(0)
let disposed = false
let pendingTimer: number | null = null
let requestGeneration = 0

class PreviewPageError extends Error {}

const activeDay = computed(() => view.value?.days[activeIndex.value] ?? null)
const dayLevelIssues = computed(() => activeDay.value?.issues.filter((issue) => issue.scope === 'day') ?? [])

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    pendingTimer = window.setTimeout(() => {
      pendingTimer = null
      resolve()
    }, milliseconds)
  })
}

async function loadLatest(): Promise<void> {
  const generation = ++requestGeneration
  const access = getLatestPreviewAccess()
  if (!access) {
    pageState.value = 'missing_access'
    return
  }
  pageState.value = 'loading'
  errorMessage.value = ''
  progressLabel.value = '正在整理旅行需求'
  view.value = null
  try {
    await loadUntilReady(access, generation)
  } catch (error) {
    if (disposed || generation !== requestGeneration) return
    errorMessage.value = error instanceof PreviewPageError ? error.message : v4PreviewErrorMessage(error)
    pageState.value = 'failed'
  }
}

async function loadUntilReady(access: V4PreviewAccess, generation: number): Promise<void> {
  while (!disposed && generation === requestGeneration) {
    const status = await getV4TaskStatus(access)
    if (disposed || generation !== requestGeneration) return
    progressLabel.value = stageLabel(status.stage)
    if (status.task_id !== access.taskId || status.plan_id !== access.planId) {
      throw new PreviewPageError('无法恢复这份预览。请从首页重新开始。')
    }
    if (status.status === 'failed') {
      throw new PreviewPageError('这份行程暂时没有可用预览，请稍后重试。')
    }
    if (status.status === 'interrupted') {
      throw new PreviewPageError('行程准备已中断，请返回首页重新开始。')
    }
    if (status.preview_ready || status.status === 'ready') {
      try {
        const envelope: V4PreviewEnvelope = await getV4Preview(access)
        if (disposed || generation !== requestGeneration) return
        if (envelope.task_id !== access.taskId || envelope.plan_id !== access.planId) {
          throw new PreviewPageError('无法恢复这份预览。请从首页重新开始。')
        }
        view.value = mapV4Preview(envelope)
        activeIndex.value = 0
        pageState.value = 'ready'
        return
      } catch (error) {
        if (disposed || generation !== requestGeneration) return
        if (!isPreviewNotReady(error)) throw error
      }
    }
    await wait(1800)
    if (disposed || generation !== requestGeneration) return
  }
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    accepted: '正在准备行程',
    planning_input: '正在整理旅行需求',
    dispatch: '正在查找地点与天气',
    proposal: '正在安排每天的行程',
    current_routes: '正在核实交通路线',
    timing: '正在核对游览时间与返程安排',
    validation: '正在检查每天的安排',
    repair: '正在补齐尚未安排的时段',
    cost: '正在整理消费参考',
    ready: '行程已准备好',
    failed: '行程暂时无法完成',
    interrupted: '行程准备已中断',
  }
  return labels[stage] ?? '正在准备行程'
}

function isPreviewNotReady(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'response' in error
    && (error as { response?: { status?: number; data?: { detail?: { code?: string } } } }).response?.status === 409
    && (error as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code === 'PREVIEW_NOT_READY'
}

function formatDate(value: string, timezone: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value.slice(0, 10)
  return new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', timeZone: timezone }).format(date)
}

function formatShortDate(value: string): string {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return match ? `${Number(match[2])}月${Number(match[3])}日` : value
}

function activityCountText(count: number): string {
  return count === 0 ? '当天没有具体活动安排。' : `按顺序安排 ${count} 项活动。`
}

function activityKind(kind: string): string {
  if (kind === 'meal') return '用餐安排'
  if (kind === 'free_time') return '自由活动'
  return '景点游览'
}

function costGapText(unknownCount: number, conflictCount: number): string {
  const parts = []
  if (unknownCount) parts.push(`${unknownCount} 项未查到`)
  if (conflictCount) parts.push(`${conflictCount} 项存在冲突`)
  return `消费信息：${parts.join('，')}`
}

function issueTitle(issue: { category: string }): string {
  if (issue.category === 'known_conflict') return '可能需要调整'
  if (issue.category === 'missing_arrangement') return '安排缺项'
  if (issue.category === 'unknown_info' || issue.category === 'invalid_reference') return '信息未查到'
  return '行程提示'
}

function issueTone(issue: { category: string }): string {
  return issue.category === 'known_conflict' ? 'warning' : 'info'
}

onMounted(() => { void loadLatest() })
onUnmounted(() => {
  disposed = true
  requestGeneration++
  if (pendingTimer !== null) window.clearTimeout(pendingTimer)
})
</script>

<style scoped>
.preview-shell { max-width: 1440px; min-height: 100vh; margin: 0 auto; padding: 30px clamp(20px, 5vw, 72px) 84px; color: #1b322c; font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif; }
.page-topline { display: flex; justify-content: space-between; align-items: center; padding-bottom: 17px; border-bottom: 1px solid #d7ded2; }
.eyebrow { color: #a65c40; font-size: 10px; letter-spacing: .15em; font-weight: 700; }
.readonly-label { color: #718173; border: 1px solid #d2dbc7; padding: 5px 9px; font-size: 10px; }
.hero { display: flex; align-items: end; justify-content: space-between; gap: 30px; padding: 34px 0 27px; border-bottom: 1px solid #cbd7c6; }
.hero-kicker { color: #a65c40; font-size: 10px; letter-spacing: .17em; font-weight: 700; margin: 0; }
.hero h1 { margin: 13px 0 10px; font: 600 clamp(37px, 5vw, 58px)/1.16 'Songti SC', 'Noto Serif SC', serif; letter-spacing: -.04em; }
.date-range { margin: 0; color: #708172; font-size: 13px; }
.cost-summary { display: flex; flex-direction: column; gap: 7px; min-width: 275px; max-width: 330px; padding: 19px 23px; background: #e4edcd; color: #51664f; font-size: 11px; line-height: 1.6; }
.cost-summary strong { color: #244032; font: 34px/1.2 'Songti SC', 'Noto Serif SC', serif; }
.cost-summary small { color: #63745f; font-size: 10px; }
.global-issues { margin: 18px 0; }
.day-tabs { display: flex; gap: 2px; overflow-x: auto; margin: 24px 0 20px; border-bottom: 1px solid #d9e1d4; }
.day-tabs button { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; min-width: 150px; border: 0; border-bottom: 2px solid transparent; background: transparent; padding: 14px 17px; color: #718274; text-align: left; cursor: pointer; }
.day-tabs button.active { background: #e5edcf; border-bottom-color: #bb704f; color: #1b392e; }
.day-tabs small { color: inherit; font-size: 11px; }
.day-tabs i { color: inherit; font-size: 10px; font-style: normal; }
.day-overview { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 22px 0 19px; }
.overview-copy h2 { margin: 8px 0 6px; color: #254130; font: 27px 'Songti SC', 'Noto Serif SC', serif; }
.overview-copy p { margin: 0; color: #718173; font-size: 11px; }
.daily-facts { display: flex; justify-content: end; flex-wrap: wrap; gap: 8px; max-width: 560px; }
.daily-facts span { background: #edf2e4; padding: 8px 10px; color: #45624f; font-size: 10px; }
.day-layout { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(340px, .9fr); align-items: start; gap: 38px; }
.timeline-column { min-width: 0; }
.info-card { margin: 0 0 15px; padding: 17px 20px; border: 1px solid #d9dfd3; background: #f7f8f1; }
.card-topline { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.fact-known, .fact-unknown { color: #64785e; font-size: 10px; }
.fact-unknown { color: #9a6445; }
.info-card h3 { margin: 9px 0 5px; color: #254130; font: 20px 'Songti SC', 'Noto Serif SC', serif; }
.info-card p { margin: 6px 0 0; color: #61746a; font-size: 11px; line-height: 1.8; }
.lodging-grid { display: grid; grid-template-columns: 1.15fr 1fr; gap: 24px; margin-top: 15px; }
.lodging-grid small { color: #718173; font-size: 10px; }
.lodging-grid h3 { font-size: 17px; }
.lodging-grid .hotel-cost { display: block; margin-top: 9px; }
.cost-evidence { display: block; margin-top: 5px; color: #8a9585 !important; font-size: 9px !important; line-height: 1.6; }
.area-reference { border-left: 1px solid #dce3d3; padding-left: 20px; }
.return-card { margin: 15px 0; padding: 17px 20px; border: 1px solid #d7dfcf; background: #f5f7ef; color: #5d705c; }
.return-card.conflict { border-color: #e0c7ab; background: #fbf3e8; color: #80553d; }
.return-card.unverified { border-color: #e1d6bd; background: #fbf7eb; color: #746443; }
.return-card h3 { margin: 8px 0; font-size: 15px; font-weight: 600; }
.return-card p { margin: 8px 0 0; color: inherit; font-size: 10px; line-height: 1.7; }
.return-facts { display: flex; flex-wrap: wrap; gap: 7px; }
.return-facts span { padding: 5px 7px; background: #ffffffa8; font-size: 9px; }
.return-leg-route { margin-top: 13px; padding-top: 13px; border-top: 1px solid #dfdfd0; }
.muted { color: #718173 !important; }
.day-issues { margin: 14px 0; }
.issue-row { display: flex; align-items: flex-start; gap: 10px; margin: 7px 0; padding: 10px 13px; border: 1px solid #e1d6bd; background: #fbf6ea; color: #6e5e43; font-size: 11px; line-height: 1.7; }
.issue-row.info { border-color: #dce3d3; background: #f6f7f0; color: #63735d; }
.issue-row b { flex-shrink: 0; font-weight: 600; }
.missing-slot { display: flex; gap: 12px; margin: 13px 0; padding: 15px 17px; border: 1px dashed #c8b791; background: #fbf7eb; color: #64563c; }
.missing-icon { display: grid; flex: 0 0 22px; place-items: center; height: 22px; border-radius: 50%; background: #e5d9b8; font-size: 12px; }
.missing-icon.conditional { background: #d9e2cc; color: #526a54; }
.missing-slot b { font-size: 12px; font-weight: 600; }
.missing-slot p { margin: 4px 0 0; font-size: 11px; line-height: 1.7; }
.missing-slot .issue-detail { color: #837353; }
.leg-card { margin: 15px 0 15px 14px; padding: 0 0 0 16px; border-left: 2px solid #b2c7a6; }
.leg-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; color: #64785e; font-size: 10px; }
.leg-heading b { color: #47654f; font-size: 11px; font-weight: 600; }
.mode-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-top: 9px; }
.mode-chip { display: flex; flex-direction: column; gap: 5px; min-width: 0; padding: 8px 9px; border: 1px solid #e0e7d5; color: #718173; font-size: 9px; }
.mode-chip b { color: #475d4c; font-size: 10px; font-weight: 500; }
.mode-chip.chosen { border-color: #789272; background: #eaf1df; }
.route-note { margin: 7px 0 0; color: #788675; font-size: 9px; line-height: 1.6; }
.activity-card { margin: 0 0 14px; padding: 19px 21px; border: 1px solid #d9dfd3; background: #fffdfa; box-shadow: 5px 5px 0 #f0f2e9; }
.activity-card.meal { background: #fcfaf5; }
.activity-card.free { border-style: dashed; }
.activity-meta { display: flex; justify-content: space-between; gap: 12px; color: #a75b3f; font-size: 10px; }
.activity-meta span + span { color: #61746a; }
.activity-title { display: flex; align-items: center; gap: 12px; margin: 14px 0 9px; }
.activity-number, .destination-number { display: grid; flex: 0 0 23px; place-items: center; width: 23px; height: 23px; border-radius: 50%; color: #688060; background: #edf2e4; font-size: 10px; }
.activity-title h3 { margin: 0 0 4px; color: #254130; font: 21px 'Songti SC', 'Noto Serif SC', serif; }
.activity-title small { color: #718173; font-size: 10px; }
.activity-description, .activity-address { margin: 7px 0; color: #61746a; font-size: 11px; line-height: 1.8; }
.activity-address { color: #81907d; font-size: 10px; }
.activity-card footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-top: 13px; padding-top: 11px; border-top: 1px solid #e9eddf; color: #6a7f60; font-size: 10px; }
.map-column { position: sticky; top: 20px; min-width: 0; }
.destination-card { margin-top: 18px; padding: 19px 21px; border: 1px solid #d8e0d2; background: #fffdfa; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; }
.section-heading h3 { margin: 7px 0 0; color: #254130; font: 20px 'Songti SC', 'Noto Serif SC', serif; }
.section-heading > small { color: #718173; font-size: 10px; }
.destination-list, .candidate-list { list-style: none; padding: 0; margin: 13px 0 0; }
.destination-list li { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid #edf0e9; }
.destination-list li > span:nth-child(2) { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 4px; }
.destination-list b, .candidate-list b { color: #355342; font-size: 11px; font-weight: 500; }
.destination-list small, .candidate-list small { color: #718173; font-size: 9px; line-height: 1.6; }
.coord-missing { flex: 0 0 auto; color: #a16c51 !important; }
.recommendations > p { margin: 8px 0; font-size: 10px; }
.candidate-list li { padding: 12px 0; border-bottom: 1px solid #edf0e9; }
.candidate-list li > div { display: flex; align-items: center; gap: 8px; }
.candidate-type { padding: 3px 6px; background: #edf2e4; color: #6b805e; font-size: 9px; }
.candidate-list li > small { display: block; margin: 5px 0; }
.candidate-list p { margin: 5px 0 0; color: #718173; font-size: 10px; line-height: 1.7; }
.empty-day { padding: 24px; border: 1px dashed #b7c9a8; color: #748467; }
.empty-day h3 { font: 19px 'Songti SC', 'Noto Serif SC', serif; }
.empty-day p { font-size: 11px; }
.state-page { display: flex; min-height: 72vh; align-items: center; justify-content: center; flex-direction: column; text-align: center; }
.state-page h1 { margin: 13px 0 8px; color: #254130; font: 32px 'Songti SC', 'Noto Serif SC', serif; }
.state-page p { max-width: 470px; margin: 0; color: #718173; font-size: 12px; line-height: 1.8; }
.quiet-button { margin-top: 20px; border: 1px solid #cdd8c9; background: #fffdfa; padding: 9px 15px; color: #355b44; cursor: pointer; }
.quiet-link { display: inline-block; margin-top: 20px; border: 1px solid #cdd8c9; background: #fffdfa; padding: 9px 15px; color: #355b44; text-decoration: none; font-size: 12px; }
.state-actions { display: flex; align-items: center; gap: 10px; }
.state-actions .quiet-button, .state-actions .quiet-link { margin-top: 20px; }
.loading-line { display: flex; gap: 7px; margin-top: 23px; }
.loading-line i { width: 6px; height: 6px; border-radius: 50%; background: #92a688; animation: pulse 1.1s infinite ease-in-out; }
.loading-line i:nth-child(2) { animation-delay: .15s; }.loading-line i:nth-child(3) { animation-delay: .3s; }
@keyframes pulse { 0%, 80%, 100% { opacity: .35; transform: translateY(0); } 40% { opacity: 1; transform: translateY(-4px); } }
@media (max-width: 940px) { .day-layout { grid-template-columns: 1fr; }.map-column { position: static; order: -1; }.hero { align-items: start; }.daily-facts { max-width: 430px; } }
@media (max-width: 620px) { .preview-shell { padding: 20px 16px 55px; }.hero { display: block; padding: 25px 0; }.hero h1 { font-size: 39px; }.cost-summary { margin-top: 19px; min-width: 0; max-width: none; }.day-overview { display: block; }.daily-facts { justify-content: start; margin-top: 12px; }.lodging-grid { grid-template-columns: 1fr; gap: 13px; }.area-reference { border-left: 0; border-top: 1px solid #dce3d3; padding: 13px 0 0; }.mode-list { gap: 5px; }.mode-chip { padding: 7px; }.activity-card { padding: 16px; }.activity-title h3 { font-size: 19px; }.map-column { order: 0; } }
</style>
