<template>
  <main class="result-shell">
    <nav class="result-nav">
      <router-link to="/">← 调整全局需求，重新规划</router-link>
      <div class="nav-actions">
        <router-link :to="{ path: '/observability', query: taskId ? { task: taskId } : {} }">查看规划过程</router-link>
        <TripImageExport :plan="exportPlan" :statuses="statusMap" />
      </div>
    </nav>

    <header class="hero">
      <div>
        <span class="eyebrow">每日旅行路线</span>
        <h1>{{ plan.planning_conditions.city }} · {{ plan.days.length }} 天</h1>
        <p>{{ plan.planning_conditions.start_date }} — {{ plan.planning_conditions.end_date }}</p>
      </div>
      <div class="budget">
        <small>{{ currentPlan.cost_summary.complete ? '查询到的费用合计 / 人' : '当前可计算费用小计 / 人' }}</small>
        <strong>¥{{ currentPlan.cost_summary.known_total }}</strong>
        <small v-if="!currentPlan.cost_summary.complete">部分价格未查询到，不代表全程总价</small>
        <small v-if="dirtyCostDays.length">不含正在更新的{{ dirtyCostDays.join('、') }}</small>
        <small v-if="plan.planning_conditions.budget_per_person != null">参考预算 ¥{{ plan.planning_conditions.budget_per_person }} / 人</small>
      </div>
    </header>

    <section class="base">
      <div>
        <strong>{{ plan.lodging_base.source === 'user' ? '已定住处' : '推荐住宿区域' }}</strong>
        <h3>{{ plan.lodging_base.user_input || plan.lodging_base.area_name || '暂无住宿信息' }}</h3>
        <p>{{ plan.lodging_base.recommendation_reason }}</p>
      </div>
      <div class="global-conditions">
        <p>到达：{{ formatDateTime(plan.planning_conditions.arrival_at) }}<span v-if="plan.planning_conditions.arrival_place"> · {{ plan.planning_conditions.arrival_place.name }}</span></p>
        <p>离开：{{ formatDateTime(plan.planning_conditions.departure_at) }}<span v-if="plan.planning_conditions.departure_place"> · {{ plan.planning_conditions.departure_place.name }}</span></p>
        <small>交通耗时为路线参考，不提供导航。</small>
      </div>
    </section>

    <section v-if="adjustmentIssues.length || missingIssues.length" class="issues">
      <details v-if="adjustmentIssues.length" open>
        <summary>可能需要调整 · {{ adjustmentIssues.length }} 项</summary>
        <div class="issue-list"><button v-for="issue in adjustmentIssues" :key="issue.issue_id" @click="locate(issue)">{{ issue.date ? `${issue.date} · ` : '' }}{{ issue.message }}</button></div>
      </details>
      <details v-if="missingIssues.length" open>
        <summary>信息未查到 · {{ missingIssues.length }} 项</summary>
        <div class="issue-list muted-list"><button v-for="issue in missingIssues" :key="issue.issue_id" @click="locate(issue)">{{ issue.date ? `${issue.date} · ` : '' }}{{ issue.message }}</button></div>
      </details>
    </section>

    <div class="day-tabs" role="tablist" aria-label="旅行日期">
      <button v-for="(item,index) in plan.days" :key="item.date" role="tab" :aria-selected="index === active" :class="{ active: index === active }" @click="selectDay(index)">
        第 {{ index+1 }} 天 <small>{{ item.date }}</small>
      </button>
    </div>

    <div v-if="day" class="day-layout">
      <section class="timeline">
        <div class="day-heading"><div><h2>{{ day.date }} 的安排</h2><p class="muted">{{ day.description }}</p></div><span v-if="editStatus !== 'idle'" :class="['edit-state', editStatus]">{{ editStatus === 'updating' ? '路线更新中' : '路线更新失败，保留当前景点' }}</span></div>
        <div class="daily-summary">
          <span v-if="derivedVisible">已知安排 {{ day.time_summary.known_minutes ?? '—' }} 分钟</span>
          <span v-else>当前路线时间正在更新或尚未更新</span>
          <span v-if="editStatus === 'idle'">已知费用 ¥{{ day.cost_summary.known_total }} / 人<span v-if="!day.cost_summary.complete">（部分未查到）</span></span>
          <span v-else>本日费用小计待更新</span>
          <span v-if="day.cost_summary.unknown_count && editStatus === 'idle'">未查询到 {{ day.cost_summary.unknown_count }} 项费用</span>
        </div>
        <p v-if="editError" class="edit-error">{{ editError }} <button @click="retryEdits">重试路线更新</button></p>
        <p v-if="day.time_summary.possible_overrun_minutes != null && day.time_summary.possible_overrun_minutes > 0 && editStatus === 'idle'" class="notice">按当前预估，可能超时约 {{ day.time_summary.possible_overrun_minutes }} 分钟。</p>
        <p v-else-if="day.time_summary.unknown_leg_count > 0 && editStatus === 'idle'" class="notice">部分路段耗时未查到，暂时不能完整判断是否可能超时。</p>
        <div v-if="day.weather" class="weather">天气参考：{{ day.weather.dayweather }} · {{ day.weather.nighttemp }}–{{ day.weather.daytemp }}℃</div>
        <div v-else class="muted">未查询到当天预报。</div>
        <div v-if="day.activities.length === 0" class="empty-day">当天暂无景点，可在右侧地图搜索后加入。</div>
        <template v-for="(activity,index) in day.activities" :key="activity.activity_id">
          <section v-for="leg in arriving(activity.activity_id)" :key="leg.leg_id" :id="leg.leg_id" class="leg-card">
            <div class="leg-heading"><strong>{{ leg.origin?.name || '上一地点' }} → {{ leg.destination?.name || activity.title }}</strong><small v-if="leg.fastest_mode && derivedVisible">最快：{{ modeName(leg.fastest_mode) }}</small></div>
            <p v-if="!derivedVisible" class="muted">路线耗时暂不可用</p>
            <div class="mode-options">
              <button v-for="mode in availableModes()" :key="mode" :class="{ selected: selectedMode(leg) === mode, fastest: leg.fastest_mode === mode }" :disabled="!canEdit || !isRouteOptionAvailable(leg.options?.[mode])" @click="selectMode(leg, mode)">
                <span>{{ modeName(mode) }}</span>
                <b>{{ derivedVisible ? formatMinutes(leg.options?.[mode]?.duration_minutes ?? null) : '—' }}</b>
                <small v-if="selectedMode(leg) === mode">当前采用</small>
                <small v-else-if="leg.fastest_mode === mode">最快</small>
                <small v-else-if="!isRouteOptionAvailable(leg.options?.[mode])">暂无耗时数据</small>
              </button>
            </div>
          </section>
          <article :id="activity.activity_id" class="activity">
            <div class="activity-top"><span class="period">{{ derivedVisible ? periodName(activity) : '时段与时刻待更新' }}<span v-if="derivedVisible && (activity.start_at || activity.end_at)"> · {{ activityTime(activity) }}</span></span><span>建议停留：{{ activity.duration_minutes == null ? '未查询到' : `约 ${activity.duration_minutes} 分钟` }}</span></div>
            <div class="activity-title"><button v-if="activity.place?.source_id" class="activity-name" @click="showActivity(activity)">{{ index + 1 }}. {{ activity.title }}</button><h3 v-else>{{ index + 1 }}. {{ activity.title }}</h3><div class="activity-actions">
              <button :disabled="!canEdit || index === 0" aria-label="上移景点" @click="moveActivity(activity, 'up')">↑</button>
              <button :disabled="!canEdit || index === day.activities.length - 1" aria-label="下移景点" @click="moveActivity(activity, 'down')">↓</button>
              <button :disabled="!canEdit" class="danger" @click="confirmDelete(activity)">删除</button>
            </div></div>
            <p>{{ activity.description }}</p>
            <p v-if="activity.place" class="address">{{ activity.place.name }} · {{ activity.place.address }}</p>
            <p class="opening-hours">开放时间：{{ activity.opening_hours || '未查询到' }}</p>
            <div v-if="activity.photos?.length" class="photo-strip">
              <template v-for="photo in activity.photos.slice(0, 3)" :key="photo"><img v-if="!failedPhotos[photo]" :src="photo" :alt="`${activity.title}地点图片`" @error="failedPhotos[photo] = true"><span v-else class="photo-failed">图片加载失败</span></template>
            </div>
            <footer><span>{{ costText(activity) }}</span><small>{{ referenceCostUnit(activity) }}</small></footer>
          </article>
        </template>
        <section v-for="leg in leavingLegs" :key="leg.leg_id" :id="leg.leg_id" class="leg-card">
          <div class="leg-heading"><strong>{{ leg.origin?.name || '最后地点' }} → {{ leg.destination?.name || '离开地点' }}</strong><small v-if="leg.fastest_mode && derivedVisible">最快：{{ modeName(leg.fastest_mode) }}</small></div>
          <p v-if="!derivedVisible" class="muted">路线耗时暂不可用</p>
          <div class="mode-options"><button v-for="mode in availableModes()" :key="mode" :class="{ selected: selectedMode(leg) === mode, fastest: leg.fastest_mode === mode }" :disabled="!canEdit || !isRouteOptionAvailable(leg.options?.[mode])" @click="selectMode(leg, mode)"><span>{{ modeName(mode) }}</span><b>{{ derivedVisible ? formatMinutes(leg.options?.[mode]?.duration_minutes ?? null) : '—' }}</b><small v-if="selectedMode(leg) === mode">当前采用</small><small v-else-if="leg.fastest_mode === mode">最快</small><small v-else-if="!isRouteOptionAvailable(leg.options?.[mode])">暂无耗时数据</small></button></div>
        </section>
      </section>

      <aside class="map-panel">
        <h2>当天地图与地点搜索</h2>
        <div id="route-map" class="live-map" v-show="mapReady"></div>
        <svg v-if="!mapReady" class="route-sketch" viewBox="0 0 480 320" role="img" aria-label="当天地点位置示意图">
          <path d="M0 80H480 M0 160H480 M0 240H480 M80 0V320 M160 0V320 M240 0V320 M320 0V320 M400 0V320" stroke="#dce8e6" fill="none" />
          <polyline :points="points.map(point => `${point.x},${point.y}`).join(' ')" fill="none" stroke="#5b8f86" stroke-width="2" stroke-dasharray="5 5" />
          <g v-for="(point,index) in points" :key="`${point.name}-${index}`"><circle :cx="point.x" :cy="point.y" r="12" fill="#147d6b"/><text :x="point.x" :y="point.y+4" text-anchor="middle" fill="white" font-size="10">{{ index + 1 }}</text><text :x="point.x" :y="point.y+26" text-anchor="middle" font-size="10">{{ point.name.slice(0,10) }}</text></g>
          <g v-if="selectedMapPoint"><circle :cx="selectedMapPoint.x" :cy="selectedMapPoint.y" r="10" fill="#d28a39"/><text :x="selectedMapPoint.x" :y="selectedMapPoint.y+26" text-anchor="middle" font-size="10" fill="#8a5720">搜索位置</text></g>
        </svg>
        <p class="muted">虚线仅表示地点顺序，不是导航线路。搜索范围固定为 {{ plan.planning_conditions.city }}。</p>
        <a-input-search v-model:value="searchText" placeholder="在目的地搜索地点" allow-clear />
        <div v-if="searchLoading" class="muted search-status">正在搜索地点…</div>
        <div v-else-if="searchError" class="search-error">{{ searchError }}</div>
        <ul v-else-if="searchResults.length" class="search-results">
          <li v-for="poi in searchResults" :key="poi.id"><button class="search-hit" @click="showPoi(poi)"><strong>{{ poi.name }}</strong><small>{{ poi.address || poi.type || '地点信息' }}</small></button></li>
        </ul>
        <section v-if="selectedPoi" class="poi-detail">
          <h3>{{ selectedPoi.name }}</h3><p>{{ selectedPoi.type || '地点' }}</p><p>{{ selectedPoi.address || '未查询到详细地址' }}</p><p v-if="selectedPoi.tel">电话：{{ selectedPoi.tel }}</p>
          <p>开放时间：{{ selectedPoi.opening_hours || '未查询到' }}</p>
          <p>{{ selectedPoi.reference_cost == null ? '未查询到真实参考费用' : `地图参考费用 ¥${selectedPoi.reference_cost}${poiCostBasis(selectedPoi.cost_basis) ? ` · ${poiCostBasis(selectedPoi.cost_basis)}` : ''}` }}</p>
          <div v-if="selectedPoi.photos?.length" class="photo-strip poi-photos"><template v-for="photo in selectedPoi.photos.slice(0, 3)" :key="photo"><img v-if="!failedPhotos[photo]" :src="photo" :alt="`${selectedPoi.name}地点图片`" @error="failedPhotos[photo] = true"><span v-else class="photo-failed">图片加载失败</span></template></div>
          <p v-if="sameDayDuplicate" class="duplicate">已加入当天</p>
          <p v-else-if="otherDayNames.length" class="muted">已安排在{{ otherDayNames.join('、') }}，仍可加入当天。</p>
          <a-button type="primary" block :disabled="!canEdit || sameDayDuplicate" @click="addPoi(selectedPoi)">{{ sameDayDuplicate ? '已加入当天' : `加入第 ${active + 1} 天` }}</a-button>
        </section>
        <p v-if="!canEdit" class="muted">当前行程缺少编辑任务凭据，只能查看。</p>
      </aside>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import TripImageExport from './TripImageExport.vue'
import { errorText, getPoiDetail, recalculateDay, searchPois } from '@/services/api'
import { applyOptimisticEdits, copyDay, isRouteOptionAvailable, makeOptimisticActivity, aggregateSummaries, TRAVEL_MODE_ORDER, visibleCost, type PendingDayEdit } from '@/services/dayEditing'
import { compactCancelledPendingAdds } from '@/services/pendingDayEdits'
import type { Activity, DayEditOperation, Issue, Leg, Place, PoiSearchResult, RouteDay, RouteItinerary, TravelMode } from '@/types/itinerary'

const props = defineProps<{ initialPlan: RouteItinerary; taskId: string }>()
const plan = ref<RouteItinerary>(JSON.parse(JSON.stringify(props.initialPlan)))
const active = ref(0)
const sessions = reactive<Record<string, DaySession>>({})
const searchText = ref('')
const searchResults = ref<PoiSearchResult[]>([])
const searchLoading = ref(false)
const searchError = ref('')
const selectedPoi = ref<PoiSearchResult | null>(null)
const failedPhotos = reactive<Record<string, boolean>>({})
const mapReady = ref(false)
const mapNote = ref('地点图示仅用于查看位置与顺序。')
const detailGeneration = ref(0)
let searchGeneration = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let map: any = null
let amap: any = null
let disposed = false

interface DaySession {
  accepted: RouteDay
  viewDay: RouteDay
  pending: PendingDayEdit[]
  inFlight: PendingDayEdit[]
  revision: number
  status: 'idle' | 'updating' | 'failed'
  error: string
  timer?: ReturnType<typeof setTimeout>
}

const baseDay = (date: string) => plan.value.days.find(item => item.date === date)
function ensureSession(date: string): DaySession | undefined {
  if (sessions[date]) return sessions[date]
  const day = baseDay(date)
  if (!day) return undefined
  const session: DaySession = { accepted: copyDay(day), viewDay: copyDay(day), pending: [], inFlight: [], revision: 0, status: 'idle', error: '' }
  sessions[date] = session
  return session
}
function refreshSession(session: DaySession) {
  session.viewDay = applyOptimisticEdits(session.accepted, [...session.inFlight, ...session.pending])
}
const day = computed(() => {
  const initial = plan.value.days[active.value]
  return initial ? sessions[initial.date]?.viewDay || initial : null
})
const editStatus = computed(() => day.value ? sessions[day.value.date]?.status || 'idle' : 'idle')
const editError = computed(() => day.value ? sessions[day.value.date]?.error || '' : '')
const derivedVisible = computed(() => editStatus.value === 'idle')
const canEdit = computed(() => !!props.taskId && !!day.value?.edit_token)
const currentDays = computed(() => plan.value.days.map(item => sessions[item.date]?.viewDay || item))
const currentPlan = computed<RouteItinerary>(() => ({
  ...plan.value,
  days: currentDays.value.map(item => sessions[item.date]?.status && sessions[item.date].status !== 'idle' ? { ...item, issues: [] } : item),
  cost_summary: aggregateSummaries(currentDays.value.map(item => {
    const status = sessions[item.date]?.status
    return status && status !== 'idle' ? { ...item.cost_summary, known_total: 0, unknown_count: item.cost_summary.unknown_count + 1, complete: false } : item.cost_summary
  }))
}))
const statusMap = computed(() => Object.fromEntries(plan.value.days.map(item => [item.date, sessions[item.date]?.status || 'idle'])))
const dirtyCostDays = computed(() => plan.value.days.flatMap((item, index) => sessions[item.date]?.status && sessions[item.date].status !== 'idle' ? [`第 ${index + 1} 天`] : []))
const allIssues = computed(() => {
  const changedDates = new Set(Object.entries(sessions).filter(([, session]) => session.status !== 'idle').map(([date]) => date))
  const isBudgetIssue = (issue: Issue) => issue.code.startsWith('BUDGET_')
  const globalIssues = plan.value.issues.filter(issue => !issue.date && !isBudgetIssue(issue))
  const stableIssues = plan.value.issues.filter(issue => !!issue.date && !changedDates.has(issue.date) && !isBudgetIssue(issue))
  const currentDayIssues = currentDays.value.filter(item => !changedDates.has(item.date)).flatMap(item => item.issues || []).filter(issue => !isBudgetIssue(issue))
  const budget = plan.value.planning_conditions.budget_per_person
  const knownTotal = currentPlan.value.cost_summary.known_total
  const budgetIssue: Issue[] = budget != null && knownTotal > budget ? [{ issue_id: 'current-budget-overrun', code: 'BUDGET_OVER', category: 'needs_adjustment', date: null, activity_id: null, leg_id: null, message: `已知参考费用 ¥${knownTotal} 已超过人均预算 ¥${budget}${currentPlan.value.cost_summary.complete ? '' : '，且仍有费用未查询到'}` }] : []
  return [...globalIssues, ...stableIssues, ...currentDayIssues, ...budgetIssue]
    .filter((issue, index, items) => items.findIndex(other => other.issue_id === issue.issue_id) === index)
})
const adjustmentIssues = computed(() => allIssues.value.filter(issue => issue.category === 'needs_adjustment'))
const missingIssues = computed(() => allIssues.value.filter(issue => issue.category !== 'needs_adjustment'))
const exportPlan = computed<RouteItinerary>(() => ({ ...currentPlan.value, issues: allIssues.value }))

function daySession(date: string) { return ensureSession(date) }
function requestId() { return globalThis.crypto?.randomUUID?.() || `edit-${Date.now()}-${Math.random().toString(16).slice(2)}` }
function modeName(mode: string) { return ({ walking: '步行', bicycling: '骑行', cycling: '骑行', transit: '公共交通', driving: '驾车' } as Record<string, string>)[mode] || mode }
function selectedMode(leg: Leg): TravelMode | undefined { return (leg.selected_mode || leg.mode) as TravelMode | undefined }
const includeDriving = computed(() => {
  const conditions = plan.value.planning_conditions
  return /自驾|开车|driving/i.test(`${conditions.transportation} ${conditions.remarks || ''}`) || currentDays.value.some(item => item.legs.some(leg => !!leg.options?.driving))
})
function availableModes(): TravelMode[] {
  return TRAVEL_MODE_ORDER.filter(mode => mode !== 'driving' || includeDriving.value)
}
function formatMinutes(value: number | null) { return value == null ? '耗时未知' : `约 ${value} 分钟` }
function formatDateTime(value: string) { return value ? value.replace('T', ' ').replace(/([+-]\d\d:\d\d|Z)$/, '') : '未提供' }
function periodName(activity: Activity) {
  const hour = activity.start_at ? Number(activity.start_at.slice(11, 13)) : Number.NaN
  if (activity.type === 'meal') {
    if (Number.isFinite(hour)) return hour < 16 ? '午餐' : '晚餐'
    return activity.period === 'lunch' ? '午餐' : activity.period === 'dinner' ? '晚餐' : '用餐'
  }
  if (Number.isFinite(hour)) return hour < 12 ? '上午' : hour < 18 ? '下午' : '晚间'
  return ({ morning: '上午', lunch: '中午', afternoon: '下午', dinner: '傍晚', evening: '晚间' } as Record<string, string>)[activity.period] || activity.period
}
function costText(activity: Activity) { return visibleCost(activity.reference_cost) }
function activityTime(activity: Activity) { return [activity.start_at, activity.end_at].filter(Boolean).map(value => value!.slice(11, 16)).join('–') }
function poiCostBasis(value?: string | null) {
  if (!value || value === 'reference') return ''
  return ({ room_night: '每房每晚', per_person: '每人', person: '每人' } as Record<string, string>)[value] || value
}
function referenceCostUnit(activity: Activity) {
  const unit = activity.reference_cost?.unit || activity.reference_cost?.basis
  return poiCostBasis(unit)
}
function poiFromPlace(place: Place, activity?: Activity): PoiSearchResult {
  return { id: place.source_id, name: place.name, type: activity?.type || '', address: place.address, location: { longitude: place.longitude, latitude: place.latitude }, opening_hours: activity?.opening_hours || undefined, photos: activity?.photos || [], reference_cost: activity?.reference_cost?.amount ?? null, cost_basis: activity?.reference_cost?.unit || activity?.reference_cost?.basis || undefined }
}
function showActivity(activity: Activity) {
  if (activity.place?.source_id) void showPoi(poiFromPlace(activity.place, activity))
}

function appendEdit(date: string, edit: PendingDayEdit) {
  const session = daySession(date)
  if (!session) return
  session.pending.push(edit)
  const baseOrder = applyOptimisticEdits(session.accepted, session.inFlight).activities.map(activity => activity.activity_id)
  session.pending = compactCancelledPendingAdds(session.pending, baseOrder).edits
  session.revision++
  if (!session.pending.length && !session.inFlight.length) {
    clearTimeout(session.timer)
    session.timer = undefined
    session.status = 'idle'
    session.error = ''
    session.viewDay = copyDay(session.accepted)
    return
  }
  session.status = 'updating'
  session.error = ''
  refreshSession(session)
  clearTimeout(session.timer)
  session.timer = setTimeout(() => { session.timer = undefined; void flushEdits(date) }, 400)
}

async function flushEdits(date: string) {
  const session = sessions[date]
  if (!session || !session.pending.length || session.inFlight.length || session.status === 'failed') return
  const dayToken = session.accepted.edit_token
  if (!props.taskId || !dayToken) {
    session.status = 'failed'
    session.error = '缺少行程编辑凭据，无法更新路线。'
    return
  }
  const batch = session.pending.splice(0, 20)
  const revision = session.revision
  const id = requestId()
  session.inFlight = batch
  session.status = 'updating'
  session.error = ''
  refreshSession(session)
  try {
    const response = await recalculateDay({
      task_id: props.taskId,
      date,
      edit_token: dayToken,
      client_revision: revision,
      request_id: id,
      operations: batch.map(edit => edit.operation)
    })
    if (response.request_id !== id || response.client_revision !== revision || response.date !== date) {
      throw new Error('路线更新响应与本次编辑不匹配')
    }
    session.accepted = copyDay(response.day)
    session.inFlight = []
    const index = plan.value.days.findIndex(item => item.date === date)
    if (index >= 0) plan.value.days[index] = copyDay(response.day)
    plan.value.issues = [...plan.value.issues.filter(issue => issue.date !== date), ...(response.day.issues || [])]
    refreshSession(session)
    session.status = session.pending.length ? 'updating' : 'idle'
    session.error = ''
    persistPlan()
    if (session.pending.length && !session.timer) session.timer = setTimeout(() => { session.timer = undefined; void flushEdits(date) }, 0)
  } catch (error: any) {
    session.pending = compactCancelledPendingAdds([...batch, ...session.pending], session.accepted.activities.map(activity => activity.activity_id)).edits
    session.inFlight = []
    if (!session.pending.length) {
      clearTimeout(session.timer)
      session.timer = undefined
      session.status = 'idle'
      session.error = ''
      refreshSession(session)
      return
    }
    session.status = 'failed'
    session.error = `路线更新失败：${errorText(error)}。当前景点列表已保留。`
    refreshSession(session)
  }
}

function persistPlan() {
  try {
    sessionStorage.setItem('tripPlan', JSON.stringify(plan.value))
    sessionStorage.setItem('tripTaskId', props.taskId)
  } catch { /* 存储不可用时仍保留当前页面行程 */ }
}
function retryEdits() {
  if (!day.value) return
  const date = day.value.date
  const session = sessions[date]
  if (!session || !session.pending.length) return
  session.status = 'updating'
  session.error = ''
  clearTimeout(session.timer)
  session.timer = setTimeout(() => { session.timer = undefined; void flushEdits(date) }, 0)
}

function selectMode(leg: Leg, mode: TravelMode) {
  if (!day.value || mode === selectedMode(leg) || !isRouteOptionAvailable(leg.options?.[mode])) return
  appendEdit(day.value.date, { operation: { type: 'select_leg_mode', from_activity_id: leg.from_activity_id, to_activity_id: leg.to_activity_id, mode } })
}
function moveActivity(activity: Activity, direction: 'up' | 'down') {
  if (!day.value) return
  appendEdit(day.value.date, { operation: { type: 'move_activity', activity_id: activity.activity_id, direction } })
}
function confirmDelete(activity: Activity) {
  if (!day.value) return
  const date = day.value.date
  Modal.confirm({
    title: `删除“${activity.title}”？`,
    content: activity.requirement_ids?.length ? '删除后会撤销该地点在本次行程中的必去要求。' : '确认后将从当天行程删除，并重新计算当天路线。',
    okText: '确认删除',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: () => appendEdit(date, { operation: { type: 'delete_activity', activity_id: activity.activity_id, confirmed: true } })
  })
}

function addPoi(poi: PoiSearchResult) {
  if (!day.value || sameDayDuplicate.value) return
  if (otherDayNames.value.length) message.info(`该地点已安排在${otherDayNames.value.join('、')}，仍加入当前天。`)
  const activityId = makeUuid()
  const operation: DayEditOperation = { type: 'add_poi', poi_id: poi.id, client_activity_id: activityId }
  appendEdit(day.value.date, { operation, optimisticActivity: makeOptimisticActivity(poi, activityId) })
}

function makeUuid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, token => {
    const value = Math.floor(Math.random() * 16)
    return (token === 'x' ? value : (value & 0x3) | 0x8).toString(16)
  })
}

const arriving = (activityId: string) => day.value?.legs.filter(leg => leg.to_activity_id === activityId) || []
const leavingLegs = computed(() => {
  if (!day.value) return []
  const shown = new Set(day.value.activities.flatMap(activity => arriving(activity.activity_id).map(leg => leg.leg_id)))
  return day.value.legs.filter(leg => !shown.has(leg.leg_id))
})
const mapPlaces = computed<Place[]>(() => {
  if (!day.value) return []
  const ordered: Place[] = []
  const add = (place: Place | null | undefined) => {
    if (place && !ordered.some(existing => existing.source_id === place.source_id)) ordered.push(place)
  }
  day.value.legs.forEach(leg => { add(leg.origin); add(leg.destination) })
  day.value.activities.forEach(activity => add(activity.place))
  return ordered
})
const sketchPositions = computed(() => {
  const places = [...mapPlaces.value]
  if (selectedPoi.value && !places.some(place => place.source_id === selectedPoi.value!.id)) places.push({ source: 'amap', source_id: selectedPoi.value.id, name: selectedPoi.value.name, address: selectedPoi.value.address || '', longitude: selectedPoi.value.location.longitude, latitude: selectedPoi.value.location.latitude })
  if (!places.length) return []
  const xs = places.map(place => place.longitude), ys = places.map(place => place.latitude)
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2
  const cos = Math.cos(cy * Math.PI / 180)
  const width = Math.max((Math.max(...xs) - Math.min(...xs)) * cos, Math.max(...ys) - Math.min(...ys), 0.005)
  return places.map(place => ({ name: place.name, x: 240 + (place.longitude - cx) * cos / width * 300, y: 150 - (place.latitude - cy) / width * 220 }))
})
const points = computed(() => sketchPositions.value.slice(0, mapPlaces.value.length))
const selectedMapPoint = computed(() => selectedPoi.value && !mapPlaces.value.some(place => place.source_id === selectedPoi.value!.id) ? sketchPositions.value[mapPlaces.value.length] : null)
const sameDayDuplicate = computed(() => !!selectedPoi.value && !!day.value?.activities.some(activity => activity.place?.source_id === selectedPoi.value!.id))
const otherDayNames = computed(() => {
  if (!selectedPoi.value) return []
  return currentDays.value.flatMap((item, index) => item.date !== day.value?.date && item.activities.some(activity => activity.place?.source_id === selectedPoi.value!.id) ? [`第 ${index + 1} 天`] : [])
})

function drawMap() {
  if (!map || !amap || !day.value) return
  map.clearMap()
  const places = mapPlaces.value
  places.forEach((place, index) => {
    const label = document.createElement('span')
    label.textContent = `${index + 1}. ${place.name}`
    const marker = new amap.Marker({ position: [place.longitude, place.latitude], title: place.name, label: { content: label.outerHTML, direction: 'top' } })
    marker.on('click', () => {
      const activity = day.value?.activities.find(item => item.place?.source_id === place.source_id)
      void showPoi(poiFromPlace(place, activity))
    })
    map.add(marker)
  })
  if (places.length > 1) map.add(new amap.Polyline({ path: places.map(place => [place.longitude, place.latitude]), strokeColor: '#5b8f86', strokeStyle: 'dashed' }))
  if (selectedPoi.value) {
    const label = document.createElement('span')
    label.textContent = `搜索：${selectedPoi.value.name}`
    const marker = new amap.Marker({ position: [selectedPoi.value.location.longitude, selectedPoi.value.location.latitude], title: selectedPoi.value.name, label: { content: label.outerHTML, direction: 'top' } })
    marker.on('click', () => { if (selectedPoi.value) void showPoi(selectedPoi.value) })
    map.add(marker)
  }
  if (places.length) map.setFitView()
  else if (selectedPoi.value) focusPlace(selectedPoi.value.location)
}
function focusPlace(place: { longitude: number; latitude: number }) {
  if (map) map.setZoomAndCenter(15, [place.longitude, place.latitude])
}
async function selectDay(index: number) {
  active.value = index
  selectedPoi.value = null
  detailGeneration.value++
  await nextTick()
  drawMap()
}
async function locate(issue: Issue) {
  if (issue.date) {
    const index = plan.value.days.findIndex(item => item.date === issue.date)
    if (index >= 0) await selectDay(index)
  }
  await nextTick()
  const element = document.getElementById(issue.leg_id || issue.activity_id || '')
  element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

watch(searchText, keyword => {
  clearTimeout(searchTimer)
  const generation = ++searchGeneration
  if (!keyword.trim()) {
    searchResults.value = []
    searchError.value = ''
    searchLoading.value = false
    return
  }
  const city = plan.value.planning_conditions.city
  searchTimer = setTimeout(async () => {
    searchLoading.value = true
    searchError.value = ''
    try {
      const results = await searchPois(keyword.trim(), city)
      if (disposed || generation !== searchGeneration || city !== plan.value.planning_conditions.city || keyword !== searchText.value) return
      searchResults.value = results
    } catch (error: any) {
      if (generation !== searchGeneration) return
      searchError.value = `搜索失败：${errorText(error)}`
      searchResults.value = []
    } finally {
      if (generation === searchGeneration) searchLoading.value = false
    }
  }, 300)
})

async function showPoi(poi: PoiSearchResult) {
  const generation = ++detailGeneration.value
  selectedPoi.value = poi
  focusPlace(poi.location)
  await nextTick()
  drawMap()
  try {
    const details = await getPoiDetail(poi.id)
    if (!disposed && generation === detailGeneration.value && selectedPoi.value?.id === poi.id) {
      selectedPoi.value = { ...poi, ...details, location: details.location || poi.location }
      focusPlace(selectedPoi.value.location)
      drawMap()
    }
  } catch { /* 保留搜索结果已有的名称、地址和坐标 */ }
}

watch(() => day.value?.activities.map(activity => activity.activity_id).join('|'), async () => {
  await nextTick()
  drawMap()
})

onMounted(async () => {
  const key = import.meta.env.VITE_AMAP_WEB_JS_KEY?.trim()
  const security = import.meta.env.VITE_AMAP_WEB_JS_SECURITY_CODE?.trim()
  if (!key || !security || key.startsWith('your_')) return
  window._AMapSecurityConfig = { securityJsCode: security }
  try {
    amap = await AMapLoader.load({ key, version: '2.0', plugins: [] })
    if (disposed) return
    mapReady.value = true
    await nextTick()
    map = new amap.Map('route-map', { zoom: 12, viewMode: '2D' })
    mapNote.value = '地图展示已核实的位置。'
    drawMap()
  } catch { mapNote.value = '地图暂不可用，地点搜索和行程编辑仍可使用。' }
})

onUnmounted(() => {
  disposed = true
  searchGeneration++
  detailGeneration.value++
  clearTimeout(searchTimer)
  Object.values(sessions).forEach(session => clearTimeout(session.timer))
  map?.destroy()
})
</script>

<style scoped>
.result-shell{max-width:1380px;margin:auto;padding:30px;color:#203a35;font-family:system-ui,sans-serif}
.result-nav,.nav-actions{display:flex;align-items:center;justify-content:space-between;gap:18px}.result-nav{margin-bottom:26px}.nav-actions{justify-content:flex-end}
a{color:#147d6b}.hero{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:22px}.eyebrow{font-size:13px;letter-spacing:2px;color:#147d6b}h1{font-size:36px;margin:8px 0}h2{font-size:21px;margin:0 0 10px}h3{font-size:18px;margin:8px 0}.hero p{color:#667c75}.budget{display:grid;text-align:right;gap:5px}.budget strong{font-size:32px}.budget small{color:#667c75}
.base{display:flex;justify-content:space-between;gap:28px;padding:20px 24px;border-radius:16px;background:#eaf3ef;margin-bottom:18px}.base p{margin:7px 0}.global-conditions{min-width:330px}.global-conditions small{color:#667c75}
.issues{display:grid;gap:10px;border:1px solid #e3d4b1;border-radius:12px;padding:14px 18px;background:#fffdf5}summary{cursor:pointer;font-weight:600}.issue-list{display:grid;grid-template-columns:1fr 1fr;max-height:180px;overflow:auto;margin-top:10px;gap:4px}.issue-list button{border:0;background:transparent;text-align:left;color:#a34f36;padding:7px;cursor:pointer}.muted-list button{color:#6b7974}
.day-tabs{display:flex;gap:8px;overflow:auto;margin:24px 0}.day-tabs button{border:1px solid #d9e3df;background:white;border-radius:12px;padding:12px 22px;cursor:pointer;white-space:nowrap;color:#36544b}.day-tabs .active{background:#24584b;color:white;border-color:#24584b}.day-tabs small{display:block;margin-top:4px}
.day-layout{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(350px,.85fr);gap:28px}.timeline{min-width:0}.day-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.muted{color:#667c75;font-size:13px;line-height:1.7}.edit-state{font-size:13px;white-space:nowrap;padding:6px 10px;border-radius:8px;background:#fff5e8;color:#a35a2e}.edit-state.failed{background:#fff0ed;color:#a43e31}
.daily-summary{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;background:#f0f5f2;padding:12px;border-radius:10px;margin:14px 0}.notice,.edit-error{color:#a34f36;font-size:13px}.edit-error button{margin-left:8px;border:0;background:transparent;color:#147d6b;text-decoration:underline;cursor:pointer}.weather{font-size:13px;margin:12px 0}.empty-day{padding:24px;margin:14px 0;text-align:center;background:#f2f6f3;border-radius:12px;color:#667c75}
.activity{border:1px solid #e0e7e2;background:white;border-radius:14px;padding:18px 22px;margin:10px 0 18px}.activity-top{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:#60756d}.period{color:#147d6b;font-weight:700}.activity-title{display:flex;justify-content:space-between;align-items:center;gap:16px}.activity-title h3,.activity-name{margin:9px 0;font-size:18px;font-weight:650}.activity-name{padding:0;border:0;background:transparent;color:#203a35;cursor:pointer;text-align:left}.activity-name:hover{text-decoration:underline;color:#147d6b}.activity p{line-height:1.8;font-size:14px}.address,.opening-hours{color:#667c75;font-size:12px!important}.activity footer{border-top:1px solid #edf0ed;padding-top:12px;display:flex;justify-content:space-between;gap:12px;font-size:12px}.activity footer small{color:#7d8984}.activity-actions{display:flex;gap:6px}.activity-actions button{min-width:32px;height:30px;border:1px solid #d4e2dc;border-radius:6px;background:#fff;color:#365c4b;cursor:pointer}.activity-actions button:disabled{opacity:.4;cursor:not-allowed}.activity-actions .danger{color:#b24336}.photo-strip{display:flex;gap:8px;overflow:hidden;margin:10px 0}.photo-strip img{width:120px;height:76px;object-fit:cover;border-radius:7px;background:#edf2ee}.photo-failed{display:grid;place-items:center;width:120px;height:76px;border-radius:7px;background:#edf2ee;color:#77847e;font-size:11px}.poi-photos img,.poi-photos .photo-failed{width:100px;height:66px}
.leg-card{display:grid;gap:8px;border-left:2px dashed #b1c5bd;margin:4px 0 12px 22px;padding:12px 14px;color:#477b6e;font-size:13px}.leg-heading{display:flex;justify-content:space-between;gap:12px}.leg-heading small{color:#9a6633}.mode-options{display:flex;flex-wrap:wrap;gap:7px}.mode-options button{display:grid;gap:3px;min-width:92px;padding:8px 10px;text-align:left;border:1px solid #d8e4de;border-radius:8px;background:white;color:#36544b;cursor:pointer}.mode-options button.selected{border-color:#16775f;background:#eff7f2}.mode-options button.fastest{box-shadow:inset 0 0 0 1px #d5a54b}.mode-options button:disabled{cursor:not-allowed;opacity:.65}.mode-options small{font-size:10px;color:#7b8a84}
.map-panel{align-self:start;position:sticky;top:16px;background:#f5f8f5;border-radius:16px;padding:20px}.live-map{height:330px;border-radius:12px;margin:12px 0}.route-sketch{width:100%;background:#eef4f1;border-radius:12px;margin:10px 0}.search-status{padding:12px}.search-error{padding:10px;color:#a34f36;font-size:13px}.search-results{list-style:none;padding:0;margin:8px 0;max-height:210px;overflow:auto;border:1px solid #e2ebe5;border-radius:8px}.search-results li+li{border-top:1px solid #edf1ee}.search-hit{display:grid;gap:4px;width:100%;border:0;background:white;text-align:left;padding:10px;cursor:pointer;color:#28483e}.search-hit:hover{background:#f2f7f3}.search-hit small{color:#667c75}.poi-detail{margin-top:14px;padding:14px;border:1px solid #dce8e1;border-radius:10px;background:white}.poi-detail p{font-size:13px;line-height:1.5}.duplicate{color:#16775f;font-weight:600}.map-panel>.muted{margin:8px 0}
@media(max-width:900px){.result-shell{padding:18px}.result-nav{align-items:flex-start}.hero,.base{display:block}.budget{text-align:left;margin-top:18px}.global-conditions{min-width:0;margin-top:16px}.day-layout{grid-template-columns:1fr}.map-panel{position:static;grid-row:1}.issue-list{grid-template-columns:1fr}h1{font-size:30px}}

/* 漫游视觉系统：保留路线编辑、搜索和导出组件的现有行为。 */
.result-shell { max-width: 1460px; padding: 44px clamp(24px, 5vw, 82px) 120px; color: #1b322c; font-family: 'DM Sans', 'Noto Sans SC', 'PingFang SC', sans-serif; }
.result-nav { margin-bottom: 42px; padding-bottom: 20px; border-bottom: 1px solid #d7ded2; font-size: 12px; letter-spacing: .035em; }
.result-nav a { color: #3f6350; text-decoration: none; transition: color .2s; }
.result-nav a:hover { color: #b96545; }
.hero { align-items: end; padding: 26px 0 44px; margin-bottom: 0; border-bottom: 1px solid #cbd7c6; }
.eyebrow { color: #a65c40; font-size: 10px; letter-spacing: .2em; font-weight: 700; }
.hero h1 { font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: clamp(42px, 5vw, 72px); line-height: 1.2; letter-spacing: -.055em; font-weight: 600; margin: 18px 0 12px; }
.hero p { margin: 0; color: #708172; font-size: 13px; letter-spacing: .04em; }
.budget { min-width: 220px; padding: 20px 26px; background: #e4edcd; text-align: left; }
.budget strong { font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 36px; color: #1b392e; line-height: 1.1; }
.budget small { color: #62735f; line-height: 1.5; }
.base { align-items: start; padding: 30px 36px; border-radius: 0; background: #eff3e8; margin: 24px 0 28px; }
.base strong { font-size: 11px; letter-spacing: .15em; color: #a65c40; }
.base h3 { font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 22px; font-weight: 600; margin: 11px 0; }
.base p, .global-conditions { color: #61746a; font-size: 13px; line-height: 1.7; }
.issues { border: 1px solid #e4d9bf; border-radius: 0; background: #fcf8ed; }
.issues summary { color: #5d513e; }
.day-tabs { gap: 0; margin: 42px 0 34px; border-bottom: 1px solid #d9e1d4; }
.day-tabs button { border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: transparent; color: #718274; padding: 13px 22px 17px; transition: color .2s, border-color .2s, background .2s; }
.day-tabs button:hover { color: #224638; background: #eef3e8; }
.day-tabs .active { border-color: #bb704f; color: #1b392e; background: #e5edcf; }
.day-layout { gap: clamp(25px, 4vw, 65px); }
.day-heading h2, .map-panel h2 { font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 27px; font-weight: 600; letter-spacing: -.035em; }
.daily-summary { border-radius: 0; background: #edf2e4; padding: 14px 17px; color: #45624f; }
.activity { border: 1px solid #d9dfd3; border-radius: 0; background: #fffdfa; padding: 22px 26px; box-shadow: 7px 7px 0 #f0f2e9; transition: transform .2s, box-shadow .2s, border-color .2s; }
.activity:hover { border-color: #a9bba5; transform: translateY(-2px); box-shadow: 8px 10px 0 #e8eee1; }
.activity-title h3, .activity-name { font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 21px; font-weight: 600; }
.activity-name:hover { color: #a65c40; }
.activity-top { color: #7a8d7d; }
.period { color: #a75b3f; }
.activity-actions button { border-radius: 2px; }
.leg-card { border-left-color: #b2c7a6; color: #526e57; }
.mode-options button { border-radius: 2px; }
.mode-options button.selected { border-color: #345f46; background: #eaf1df; }
.map-panel { top: 22px; padding: 25px; border: 1px solid #d8e0d2; border-radius: 0; background: #f4f6ee; box-shadow: 9px 9px 0 #e8eee1; }
.live-map, .route-sketch { border-radius: 0; }
.search-results, .poi-detail { border-radius: 2px; }
.search-hit:hover { background: #eef3e6; }
.photo-strip img, .photo-failed { border-radius: 2px; }
@media (max-width: 900px) {
  .result-shell { padding: 28px 26px 90px; }
  .hero { display: block; }
  .budget { width: fit-content; margin-top: 26px; }
  .base { display: block; }
  .day-layout { grid-template-columns: 1fr; }
  .map-panel { position: static; grid-row: 1; }
}
@media (max-width: 600px) {
  .result-shell { padding: 20px 20px 80px; }
  .result-nav, .nav-actions { align-items: flex-start; flex-wrap: wrap; }
  .hero h1 { font-size: 43px; }
  .budget { width: 100%; }
  .base { padding: 23px; }
  .day-tabs button { padding: 12px 15px; }
  .day-heading { display: block; }
  .activity { padding: 18px; }
  .activity-title { align-items: flex-start; }
  .map-panel { padding: 18px; box-shadow: 6px 6px 0 #e8eee1; }
}
</style>
