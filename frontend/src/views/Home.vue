<template>
  <div class="home">
    <section class="hero" aria-labelledby="hero-title">
      <div class="hero-copy">
        <div class="eyebrow"><span class="eyebrow-line"></span> THE JOURNEY STARTS HERE <span class="eyebrow-index">01 / 03</span></div>
        <h1 id="hero-title">下一程，<br />去<span class="serif-accent">有故事</span>的地方<span class="period">。</span></h1>
        <p class="hero-description">从一个念头，到一份走得通的行程。告诉我们你想去哪里，把寻找、安排与期待，交给漫游。</p>
        <a href="#plan" class="hero-cta"><span>定制我的旅程</span><span class="cta-arrow" aria-hidden="true">↗</span></a>
        <div class="hero-footnote"><span class="footnote-line"></span> EXPLORE MORE · TRAVEL DEEPER</div>
      </div>
      <div class="hero-visual">
        <img src="/images/journey-dawn.jpg" alt="日出时分的山谷与通往远方的石径" />
        <div class="image-shade"></div>
        <div class="hero-image-label"><span>FIND YOUR WAY<br />TO WONDER</span><span>COLLECT MOMENTS<br />NOT MILES</span></div>
        <div class="image-seal" aria-hidden="true"><span>GO<br />SOME<br />WHERE</span><span class="seal-star">✳</span></div>
      </div>
    </section>

    <div class="ticker" aria-hidden="true"><span>不止到达，更在沿途</span><span>✳</span><span>AN ITINERARY MADE FOR YOU</span><span>✳</span><span>不止到达，更在沿途</span><span>✳</span><span>AN ITINERARY MADE FOR YOU</span></div>

    <section id="plan" class="planner" aria-labelledby="plan-title">
      <div class="planner-intro">
        <div class="section-kicker"><span>01</span> / DESIGN YOUR JOURNEY</div>
        <h2 id="plan-title">下一段回忆，<br /><em>从这里开始。</em></h2>
        <p>你只需要给出方向。我们会结合目的地、天气与路线，把每一天串成属于你的旅行故事。</p>
        <div class="side-note"><span class="side-note-symbol">✳</span><span>少一点繁琐规划<br />多一点说走就走</span></div>
      </div>

      <div class="planner-panel">
        <div class="panel-topline"><span>YOUR TRIP DETAILS</span><span>预计填写 2 分钟</span></div>
        <a-form class="planner-form" :model="formData" layout="vertical" @finish="handleSubmit">
          <div class="form-group destination-group">
            <div class="group-heading"><span class="group-number">01</span><div><h3>目的地与时间</h3><p>每一段旅程，都有一个想去的地方。</p></div></div>
            <a-form-item name="city" :rules="[{ required: true, message: '请输入目的地城市' }]">
              <label class="field-label" for="trip-city">你想去哪里 <span>*</span></label>
              <a-input id="trip-city" v-model:value="formData.city" placeholder="输入城市，例如：成都、京都、巴黎" size="large" class="editorial-input" />
            </a-form-item>
            <div class="date-grid">
              <a-form-item name="arrival_at" :rules="[{ required: true, message: '请选择到达日期和时间' }]">
                <label class="field-label">到达时间 <span>*</span></label>
                <a-date-picker v-model:value="formData.arrival_at" show-time format="YYYY-MM-DD HH:mm" placeholder="选择到达日期和时间" size="large" class="editorial-date" popup-class-name="trip-date-popup" />
              </a-form-item>
              <a-form-item name="departure_at" :rules="[{ required: true, message: '请选择离开日期和时间' }]">
                <label class="field-label">离开时间 <span>*</span></label>
                <a-date-picker v-model:value="formData.departure_at" show-time format="YYYY-MM-DD HH:mm" placeholder="选择离开日期和时间" size="large" class="editorial-date" popup-class-name="trip-date-popup" />
              </a-form-item>
            </div>
            <div class="trip-duration" aria-live="polite"><span>旅行时长</span><strong>{{ formData.arrival_at && formData.departure_at ? formData.travel_days + ' 天' : '等待日期' }}</strong></div>
            <div class="endpoint-fields">
              <a-form-item>
                <label class="field-label">到达地点 <span class="optional">选填</span></label>
                <a-auto-complete v-model:value="endpointText.arrival" :options="arrivalOptions" :disabled="!formData.city.trim()" placeholder="机场、车站或其他地点" @search="searchArrivalEndpoint" @select="selectArrivalEndpoint" @change="endpointChanged('arrival', $event)" />
              </a-form-item>
              <a-form-item>
                <label class="field-label">离开地点 <span class="optional">选填</span></label>
                <a-auto-complete v-model:value="endpointText.departure" :options="departureOptions" :disabled="!formData.city.trim()" placeholder="搜索目的地的离开地点" @search="searchDepartureEndpoint" @select="selectDepartureEndpoint" @change="endpointChanged('departure', $event)" />
              </a-form-item>
            </div>
            <p class="field-help">从搜索结果中选择地点，才会计算该端接驳；不填写也可以继续。</p>
          </div>

          <div class="form-group preferences-group">
            <div class="group-heading"><span class="group-number">02</span><div><h3>旅行的方式</h3><p>默认适中节奏，以公共交通和步行为主。</p></div></div>
            <div class="select-grid">
              <a-form-item name="budget_per_person">
                <label class="field-label">人均参考预算 <span class="optional">全程，选填</span></label>
                <a-input-number v-model:value="formData.budget_per_person" :min="0" :precision="0" placeholder="预算金额（元）" class="editorial-number" />
              </a-form-item>
              <a-form-item name="lodging">
                <label class="field-label">已定住处 <span class="optional">选填</span></label>
                <a-input v-model:value="formData.lodging" placeholder="酒店或具体地址" :maxlength="300" />
              </a-form-item>
            </div>
            <p class="field-help">预算仅供参考；未填写住处时，会推荐住宿区域。</p>
            <div class="field-label preference-label">最期待什么 <span class="optional">可多选</span></div>
            <div class="preference-options" role="group" aria-label="旅行偏好">
              <button v-for="option in preferenceOptions" :key="option.value" type="button" class="preference-chip" :class="{ selected: formData.preferences.includes(option.value) }" :aria-pressed="formData.preferences.includes(option.value)" @click="togglePreference(option.value)">
                <span aria-hidden="true">{{ option.icon }}</span>{{ option.value }}<span class="chip-check" aria-hidden="true">{{ formData.preferences.includes(option.value) ? '✓' : '+' }}</span>
              </button>
            </div>
          </div>

          <div class="form-group last-group">
            <div class="group-heading"><span class="group-number">03</span><div><h3>还有什么想法？</h3><p>那些只有你知道的小愿望，也值得被认真安排。</p></div></div>
            <a-form-item name="free_text_input">
              <label class="field-label" for="trip-note">特别要求 <span class="optional">选填</span></label>
              <a-textarea id="trip-note" v-model:value="formData.free_text_input" placeholder="例如：想看日出、喜欢街巷里的小店、需要无障碍设施……" :rows="4" class="editorial-textarea" />
            </a-form-item>
          </div>

          <div class="form-bottom">
            <p>每一份行程，都会结合真实地点与路线。<br />提交后可查看规划过程。</p>
            <button class="submit-button" type="submit" :disabled="loading"><span>{{ loading ? '正在构思你的旅程' : '生成专属行程' }}</span><span class="submit-arrow" aria-hidden="true">{{ loading ? '◌' : '↗' }}</span></button>
          </div>
          <div v-if="loading" class="loading-note" role="status" aria-live="polite"><span class="loading-orbit" aria-hidden="true"></span><span>{{ loadingStatus }}</span></div>
          <div v-if="errorMessage" class="error-note" role="alert"><strong>行程暂时未能生成</strong><span>{{ errorMessage }}</span><span>请检查服务后再试一次，你填写的内容仍在这里。</span></div>
          <div class="task-link"><span v-if="activeTaskId">任务 {{ activeTaskId }}</span><router-link :to="{ path: '/observability', query: activeTaskId ? { task: activeTaskId } : {} }">查看任务、工程指标与调用记录 ↗</router-link></div>
        </a-form>
      </div>
    </section>

    <section class="process" aria-labelledby="process-title">
      <div class="process-heading"><div class="section-kicker"><span>02</span> / THE WAY WE WANDER</div><h2 id="process-title">从灵感，到出发。</h2></div>
      <div class="process-grid">
        <div class="process-step"><span>01 — 想去哪里</span><h3>说出你的远方</h3><p>城市、时间与偏好，就是旅程的第一张地图。</p></div>
        <div class="process-step"><span>02 — 让想法落地</span><h3>让路线自然发生</h3><p>结合当地景点、天气与交通，安排从容的每一天。</p></div>
        <div class="process-step"><span>03 — 带着期待出发</span><h3>收藏你的故事</h3><p>查看、调整并导出行程，把计划带到路上。</p></div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted, onUnmounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { submitTask, getTask, getTaskResult, errorText, stepName, searchPois } from '@/services/api'
import type { TripFormData } from '@/types'
import type { PoiSearchResult } from '@/types/itinerary'
import type { Dayjs } from 'dayjs'

const router = useRouter()
const loading = ref(false)
const activeTaskId = ref(localStorage.getItem('activeTripTask') || '')
const errorMessage = ref('')
const preferenceOptions = [
  { value: '历史文化', icon: '◈' }, { value: '自然风光', icon: '✳' },
  { value: '美食', icon: '✦' }, { value: '城市漫步', icon: '◇' },
  { value: '艺术', icon: '✺' }, { value: '休闲', icon: '☼' }
]
function togglePreference(value: string) {
  const index = formData.preferences.indexOf(value)
  if (index === -1) formData.preferences.push(value)
  else formData.preferences.splice(index, 1)
}
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const loadingStatus = ref('')

type TripFormState = Omit<TripFormData, 'arrival_at' | 'departure_at'> & {
  arrival_at: Dayjs | null
  departure_at: Dayjs | null
  arrival_place: PoiSearchResult | null
  departure_place: PoiSearchResult | null
}

const formData = reactive<TripFormState>({
  city: '',
  arrival_at: null,
  departure_at: null,
  travel_days: 1,
  transportation: '公共交通',
  accommodation: '',
  lodging: '',
  arrival_place_id: null,
  departure_place_id: null,
  arrival_place: null,
  departure_place: null,
  budget_per_person: null,
  preferences: [],
  free_text_input: ''
})
const endpointText = reactive({ arrival: '', departure: '' })
const arrivalResults = ref<PoiSearchResult[]>([])
const departureResults = ref<PoiSearchResult[]>([])
const endpointLabel = (poi: PoiSearchResult) => `${poi.name}${poi.address ? ` · ${poi.address}` : ''}`
const arrivalOptions = computed(() => arrivalResults.value.map(poi => ({ value: endpointLabel(poi), label: endpointLabel(poi) })))
const departureOptions = computed(() => departureResults.value.map(poi => ({ value: endpointLabel(poi), label: endpointLabel(poi) })))
let endpointTimers: Partial<Record<'arrival' | 'departure', ReturnType<typeof setTimeout>>> = {}
const endpointGeneration = { arrival: 0, departure: 0 }

function clearEndpointSelection(kind: 'arrival' | 'departure') {
  endpointGeneration[kind]++
  clearTimeout(endpointTimers[kind])
  const field = kind === 'arrival' ? 'arrival_place' : 'departure_place'
  const idField = kind === 'arrival' ? 'arrival_place_id' : 'departure_place_id'
  formData[field] = null
  formData[idField] = null
}
function selectEndpoint(kind: 'arrival' | 'departure', id: string) {
  const result = (kind === 'arrival' ? arrivalResults.value : departureResults.value).find(item => endpointLabel(item) === id)
  if (!result) return
  const field = kind === 'arrival' ? 'arrival_place' : 'departure_place'
  const idField = kind === 'arrival' ? 'arrival_place_id' : 'departure_place_id'
  formData[field] = result
  formData[idField] = result.id
  endpointText[kind] = endpointLabel(result)
}
function endpointChanged(kind: 'arrival' | 'departure', value: string) {
  const selected = kind === 'arrival' ? formData.arrival_place : formData.departure_place
  if (selected && value === endpointLabel(selected)) return
  clearEndpointSelection(kind)
}
function searchEndpoint(kind: 'arrival' | 'departure', keyword: string) {
  const requestGeneration = ++endpointGeneration[kind]
  clearTimeout(endpointTimers[kind])
  if (!keyword.trim() || !formData.city.trim()) {
    if (kind === 'arrival') arrivalResults.value = []
    else departureResults.value = []
    return
  }
  const searchCity = formData.city.trim()
  const searchKeyword = keyword.trim()
  endpointTimers[kind] = setTimeout(async () => {
    try {
      const results = await searchPois(searchKeyword, searchCity)
      if (disposed || requestGeneration !== endpointGeneration[kind] || searchCity !== formData.city.trim()) return
      if (kind === 'arrival') arrivalResults.value = results
      else departureResults.value = results
    } catch {
      if (requestGeneration !== endpointGeneration[kind] || searchCity !== formData.city.trim()) return
      if (kind === 'arrival') arrivalResults.value = []
      else departureResults.value = []
    }
  }, 300)
}
function searchArrivalEndpoint(keyword: string) { searchEndpoint('arrival', keyword) }
function searchDepartureEndpoint(keyword: string) { searchEndpoint('departure', keyword) }
function selectArrivalEndpoint(value: string) { selectEndpoint('arrival', value) }
function selectDepartureEndpoint(value: string) { selectEndpoint('departure', value) }
watch(() => formData.city, () => {
  clearEndpointSelection('arrival')
  clearEndpointSelection('departure')
  endpointText.arrival = ''
  endpointText.departure = ''
  arrivalResults.value = []
  departureResults.value = []
})

// 监听日期变化,自动计算旅行天数
watch([() => formData.arrival_at, () => formData.departure_at], ([start, end]) => {
  if (start && end) {
    const days = end.startOf('day').diff(start.startOf('day'), 'day') + 1
    if (days > 0 && days <= 30) {
      formData.travel_days = days
    } else if (days > 30) {
      message.warning('旅行天数不能超过30天')
      formData.departure_at = null
    } else {
      message.warning('离开日期不能早于到达日期')
      formData.departure_at = null
    }
    if (days === 1 && end.isBefore(start)) {
      message.warning('离开时间必须晚于到达时间')
      formData.departure_at = null
    }
  }
})

const poll = async () => {
  if (disposed || !activeTaskId.value) return
  try {
    const task = await getTask(activeTaskId.value)
    if (disposed) return
    const current = task.current_step
    const elapsed = current ? Math.max(0, Math.floor((Date.now() - Date.parse(current.started_at)) / 1000)) : null
    loadingStatus.value = current ? `${stepName(current.name)} · 已等待 ${elapsed} 秒` : '任务已接收，正在读取执行状态…'
    if (task.observation_incomplete) loadingStatus.value += '（观测记录不完整）'
    if (task.status === 'succeeded') {
      const response = await getTaskResult(task.task_id)
      if (disposed) return
      sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
      sessionStorage.setItem('tripTaskId', task.task_id)
      localStorage.removeItem('activeTripTask')
      loading.value = false
      errorMessage.value = ''
      await router.push('/result')
      return
    }
    if (task.status === 'failed' || task.status === 'interrupted') {
      loadingStatus.value = task.persistence_error?.message || task.error_message || '任务未完成'
      errorMessage.value = loadingStatus.value
      message.error(loadingStatus.value)
      localStorage.removeItem('activeTripTask')
      loading.value = false
      return
    }
  } catch (error: any) {
    if (disposed) return
    loadingStatus.value = `状态查询失败：${errorText(error)}；不代表规划失败。`
    if (error.response?.status === 404) {
      localStorage.removeItem('activeTripTask')
      activeTaskId.value = ''
      loading.value = false
      errorMessage.value = '任务不存在或已清理，请重新提交。'
      message.warning('任务不存在或已清理，请重新提交')
      return
    }
  }
  if (!disposed) timer = setTimeout(poll, 2000)
}

const handleSubmit = async () => {
  if (loading.value) return
  errorMessage.value = ''
  if (!formData.arrival_at || !formData.departure_at) {
    errorMessage.value = '请选择到达和离开日期时间。'
    message.error('请选择到达和离开日期时间')
    return
  }
  if (formData.departure_at.isBefore(formData.arrival_at)) {
    errorMessage.value = '离开时间必须晚于到达时间。'
    message.error('离开时间必须晚于到达时间')
    return
  }
  if (!formData.departure_at.isAfter(formData.arrival_at)) {
    errorMessage.value = '离开时间必须晚于到达时间。'
    message.error('离开时间必须晚于到达时间')
    return
  }
  loading.value = true
  loadingStatus.value = '正在提交需求…'
  try {
    const response = await submitTask({ city: formData.city, lodging: formData.lodging, preferences: formData.preferences, free_text_input: formData.free_text_input,
      arrival_at: `${formData.arrival_at.format('YYYY-MM-DDTHH:mm:ss')}+08:00`,
      departure_at: `${formData.departure_at.format('YYYY-MM-DDTHH:mm:ss')}+08:00`,
      arrival_place_id: formData.arrival_place_id,
      departure_place_id: formData.departure_place_id,
      budget_per_person: formData.budget_per_person })
    activeTaskId.value = response.task_id
    localStorage.setItem('activeTripTask', response.task_id)
    await poll()
  } catch (error: any) {
    loading.value = false
    errorMessage.value = errorText(error)
    message.error(errorMessage.value)
  }
}
onMounted(() => {
  if (activeTaskId.value) {
    loading.value = true
    void poll()
  }
})
onUnmounted(() => {
  disposed = true
  clearTimeout(timer)
  Object.values(endpointTimers).forEach(clearTimeout)
  endpointGeneration.arrival++
  endpointGeneration.departure++
})
</script><style scoped>
.home { overflow: hidden; }
.hero { min-height: min(740px, calc(100vh - 82px)); display: grid; grid-template-columns: 48% 52%; background: #f5f3ed; }
.hero-copy { padding: clamp(55px, 7.8vw, 124px) clamp(28px, 6vw, 100px) 48px clamp(28px, 6vw, 100px); display: flex; flex-direction: column; align-items: flex-start; position: relative; z-index: 1; }
.hero-copy > .eyebrow { animation: rise-in .65s .05s both; }
.hero-copy > h1 { animation: rise-in .8s .15s both; }
.hero-copy > .hero-description { animation: rise-in .8s .27s both; }
.hero-copy > .hero-cta { animation: rise-in .8s .38s both; }
@keyframes rise-in { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: translateY(0); } }
.eyebrow, .hero-footnote, .section-kicker, .panel-topline { font-size: 10px; letter-spacing: .21em; font-weight: 700; }
.eyebrow { display: flex; align-items: center; gap: 12px; white-space: nowrap; }
.eyebrow-line, .footnote-line { display: inline-block; width: 25px; height: 1px; background: #d46b46; }
.eyebrow-index { margin-left: 36px; color: #a6aaa0; }
.hero h1 { font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: clamp(45px, 4.45vw, 76px); line-height: 1.37; letter-spacing: -.07em; font-weight: 600; margin: clamp(50px, 6vw, 92px) 0 25px; white-space: nowrap; }
.serif-accent { font-style: italic; color: #a85b3f; }
.period { color: #d46b46; }
.hero-description { font-size: 15px; line-height: 2; max-width: 390px; color: #63716b; margin: 0 0 35px; }
.hero-cta { background: #1d392e; color: white; padding: 7px 7px 7px 25px; min-width: 205px; display: inline-flex; align-items: center; justify-content: space-between; gap: 25px; border-radius: 40px; text-decoration: none; font-size: 13px; font-weight: 700; transition: transform .25s, background .25s; }
.hero-cta:hover { color: white; background: #315e49; transform: translateY(-3px); }
.cta-arrow { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; background: #dce9bd; color: #1b322c; font-size: 18px; transition: transform .25s; }
.hero-cta:hover .cta-arrow { transform: rotate(45deg); }
.hero-footnote { display: flex; gap: 12px; align-items: center; margin-top: auto; padding-top: 70px; color: #8d9a8d; }
.hero-visual { position: relative; min-height: 630px; overflow: hidden; }
.hero-visual img { width: 100%; height: 100%; object-fit: cover; position: absolute; inset: 0; animation: image-reveal 1.2s ease both; }
.image-shade { position: absolute; inset: 0; background: linear-gradient(180deg, transparent 52%, rgba(12,35,30,.62)); }
.hero-image-label { position: absolute; bottom: 39px; left: 43px; right: 43px; display: flex; justify-content: space-between; align-items: end; color: white; font-size: 10px; line-height: 1.6; letter-spacing: .16em; font-weight: 700; }
.hero-image-label span:last-child { text-align: right; opacity: .7; }
.image-seal { position: absolute; top: 42px; right: 42px; width: 92px; height: 92px; background: #f5f3ed; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; transform: rotate(12deg); font-size: 10px; font-weight: 700; letter-spacing: .09em; line-height: 1.1; }
.seal-star { color: #d46b46; font-size: 14px; margin-top: 2px; }
@keyframes image-reveal { from { transform: scale(1.08); opacity: .7; } to { transform: scale(1); opacity: 1; } }
.ticker { display: flex; align-items: center; justify-content: space-around; gap: 50px; white-space: nowrap; height: 62px; background: #dce9bd; font-size: 12px; font-weight: 700; letter-spacing: .13em; }
.ticker span:nth-child(even) { color: #bc6b4d; font-size: 20px; }
.planner { padding: 64px clamp(24px, 6vw, 100px) 72px; background: #fffdfa; }
.planner-intro { max-width: 1200px; margin: 0 auto 24px; }
.planner .planner-intro h2 { font-size: clamp(28px, 3vw, 40px); margin: 12px 0; }
.planner-intro h2 br { display: none; }
.section-kicker { color: #a65e46; }
.section-kicker span { color: #1b322c; }
.planner h2, .process h2 { font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: clamp(38px, 3.8vw, 62px); font-weight: 600; line-height: 1.45; letter-spacing: -.065em; margin: 25px 0; }
.planner h2 em { font-weight: 500; color: #ab6246; }
.planner-intro > p { font-size: 14px; line-height: 1.8; max-width: 760px; color: #65716d; margin-bottom: 0; }
.side-note { display: none; margin-top: 105px; padding-top: 24px; border-top: 1px solid #dcded5; gap: 16px; align-items: center; font-size: 12px; line-height: 1.8; color: #7b887f; }
.side-note-symbol { font-size: 42px; color: #bf714e; line-height: 1; }
.planner-panel { max-width: 1200px; margin: 0 auto; border: 1px solid #dbded4; padding: clamp(20px, 2.4vw, 32px); box-shadow: 16px 16px 0 #f1f2e8; background: #fffdfa; }
.panel-topline { display: flex; justify-content: space-between; color: #a2aaa0; padding-bottom: 18px; border-bottom: 1px solid #dbded4; }
.panel-topline span:first-child { color: #436255; }
.form-group { border-bottom: 1px solid #e3e5db; padding: 24px 0 20px; }
.last-group { border-bottom: 0; }
.group-heading { display: flex; gap: 14px; margin-bottom: 20px; }
.group-number { width: 30px; height: 30px; flex: none; border-radius: 50%; background: #dce9bd; display: grid; place-items: center; font-size: 11px; font-weight: 700; }
.group-heading h3 { font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: 23px; font-weight: 600; margin: -2px 0 4px; }
.group-heading p { color: #9ca69c; font-size: 12px; margin: 0; }
.field-label { display: block; font-size: 12px; font-weight: 700; color: #485a51; letter-spacing: .04em; margin-bottom: 10px; }
.field-label > span:not(.optional) { color: #c36d4f; }
.optional { font-weight: 400; color: #a1a9a0; margin-left: 7px; }
.date-grid, .select-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.endpoint-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 26px; }
.field-help { margin: -9px 0 18px; color: #8a978c; font-size: 11px; line-height: 1.7; }
.planner :deep(.ant-form-item) { margin-bottom: 22px; }
.planner :deep(.ant-input), .planner :deep(.ant-picker), .planner :deep(.ant-select-selector), .planner :deep(.ant-input-number), .planner :deep(.ant-select .ant-select-selector) { border: 1px solid #d8ded4 !important; border-radius: 3px !important; box-shadow: none !important; background: #fffdfa !important; min-height: 49px; color: #1b322c; }
.planner :deep(.ant-input:hover), .planner :deep(.ant-picker:hover), .planner :deep(.ant-select:hover .ant-select-selector) { border-color: #7d9b7b !important; }
.planner :deep(.ant-input:focus), .planner :deep(.ant-picker-focused), .planner :deep(.ant-select-focused .ant-select-selector) { border-color: #486e55 !important; box-shadow: 0 0 0 3px rgba(104,145,100,.12) !important; }
.planner :deep(.ant-input::placeholder) { color: #a9b0a7; }
.editorial-date, .editorial-select { width: 100%; }
.editorial-number { width: 100%; }
.planner :deep(.ant-input-number-input) { height: 47px; }
.planner :deep(.ant-select-selector) { display: flex; align-items: center; }
.trip-duration { margin-top: -5px; padding: 12px 16px; display: flex; justify-content: space-between; background: #f1f5e9; color: #5b6e5d; font-size: 12px; }
.trip-duration strong { color: #1b322c; }
.preference-label { margin-bottom: 13px; }
.preference-options { display: flex; flex-wrap: wrap; gap: 9px; }
.preference-chip { border: 1px solid #d9dfd5; background: #fffdfa; color: #58665c; padding: 9px 12px; border-radius: 3px; font-size: 12px; cursor: pointer; display: inline-flex; gap: 8px; align-items: center; transition: background .2s, color .2s, border .2s, transform .2s; }
.preference-chip:hover { border-color: #658165; transform: translateY(-2px); }
.preference-chip.selected { background: #213e32; border-color: #213e32; color: #fffdfa; }
.preference-chip > span:first-child { font-size: 14px; color: #b7734e; }
.chip-check { font-size: 15px; opacity: .6; }
.planner :deep(textarea.ant-input) { padding: 14px; min-height: 105px; resize: vertical; }
.form-bottom { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-top: 24px; }
.form-bottom p { margin: 0; color: #98a39a; line-height: 1.8; font-size: 11px; }
.submit-button { border: 0; background: #b96545; color: white; min-width: 208px; padding: 7px 7px 7px 23px; display: inline-flex; justify-content: space-between; gap: 20px; align-items: center; border-radius: 30px; cursor: pointer; font-size: 13px; font-weight: 700; transition: background .2s, transform .2s; }
.submit-button:hover:not(:disabled) { background: #9e4e32; transform: translateY(-2px); }
.submit-button:disabled { opacity: .75; cursor: wait; }
.submit-arrow { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 50%; background: #f5f3ed; color: #a7593d; font-size: 19px; }
.loading-note { margin-top: 20px; display: flex; gap: 10px; align-items: center; color: #52725b; font-size: 12px; }
.error-note { margin-top: 20px; padding: 16px 18px; border-left: 3px solid #b96545; background: #fbf0e9; color: #6b4134; display: grid; gap: 5px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.error-note strong { font-size: 13px; }
.task-link { margin-top: 22px; display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 18px; color: #789080; font-size: 11px; overflow-wrap: anywhere; }
.task-link a { color: #345b49; text-decoration: none; border-bottom: 1px solid currentColor; }
.task-link a:hover { color: #b96545; }
.loading-orbit { width: 15px; height: 15px; border: 2px solid #c7d6ba; border-top-color: #52725b; border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.process { background: #e9eee3; padding: 105px clamp(28px, 6vw, 100px) 125px; }
.process h2 { margin: 19px 0 58px; }
.process-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 30px; }
.process-step { border-top: 1px solid #a5b8a5; padding-top: 24px; transition: transform .25s ease, border-color .25s ease; }
.process-step:hover { transform: translateY(-4px); border-color: #a35d44; }
.process-step > span { color: #a35d44; font-size: 11px; font-weight: 700; letter-spacing: .1em; }
.process-step h3 { font-family: 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', serif; font-size: 22px; margin: 30px 0 10px; font-weight: 600; }
.process-step p { font-size: 13px; color: #66776b; line-height: 1.8; max-width: 260px; }
@media (min-width: 1050px) {
  .planner-form { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); column-gap: 32px; }
  .destination-group { grid-column: 1; grid-row: 1 / 3; border-bottom: 0; border-right: 1px solid #e3e5db; padding-right: 32px; }
  .preferences-group { grid-column: 2; grid-row: 1; }
  .last-group { grid-column: 2; grid-row: 2; }
  .form-bottom, .loading-note, .error-note, .task-link { grid-column: 1 / -1; }
  .form-bottom { border-top: 1px solid #e3e5db; padding-top: 20px; margin-top: 4px; }
}
@media (max-width: 1150px) { .hero h1 { font-size: clamp(40px, 4.4vw, 60px); } .hero-copy { padding-left: 42px; padding-right: 30px; }  }
@media (max-width: 850px) { .hero { grid-template-columns: 1fr; } .hero-copy { padding: 68px 32px 54px; } .hero h1 { font-size: clamp(43px, 7vw, 66px); margin: 50px 0 20px; } .hero-footnote { padding-top: 55px; } .hero-visual { min-height: 440px; } .planner { padding: 48px 32px 60px; } .planner-intro { margin-bottom: 24px; } }
@media (max-width: 600px) { .hero-copy { padding: 55px 24px 40px; } .hero h1 { font-size: clamp(37px, 9.5vw, 50px); white-space: normal; margin-top: 40px; } .hero-description { font-size: 13px; } .hero-visual { min-height: 330px; } .hero-image-label { left: 22px; right: 22px; bottom: 22px; } .image-seal { width: 72px; height: 72px; top: 20px; right: 20px; font-size: 8px; } .ticker { justify-content: flex-start; padding: 0 24px; } .planner { padding: 40px 24px 52px; } .planner h2 { font-size: 30px; } .process h2 { font-size: 38px; } .planner-panel { padding: 23px; box-shadow: 8px 8px 0 #f1f2e8; } .panel-topline { font-size: 9px; } .date-grid, .select-grid, .endpoint-fields { grid-template-columns: 1fr; gap: 0; } .form-bottom { align-items: stretch; flex-direction: column; } .submit-button { width: 100%; } .process { padding: 80px 24px 90px; } .process-grid { grid-template-columns: 1fr; gap: 30px; } .process-step h3 { margin-top: 16px; } }
</style>

<style>
@media (max-width: 600px) {
  .trip-date-popup .ant-picker-date-panel,
  .trip-date-popup .ant-picker-date-panel .ant-picker-body {
    width: min(280px, calc(100vw - 113px));
  }
  .trip-date-popup .ant-picker-date-panel .ant-picker-content {
    width: calc(100% - 24px);
  }
}
@media (max-width: 385px) {
  .trip-date-popup { position: fixed !important; left: 50% !important; top: 50% !important; transform: translate(-50%, -50%); max-height: calc(100vh - 16px); overflow-y: auto; }
  .trip-date-popup .ant-picker-datetime-panel { flex-direction: column; }
  .trip-date-popup .ant-picker-date-panel,
  .trip-date-popup .ant-picker-date-panel .ant-picker-body,
  .trip-date-popup .ant-picker-time-panel { width: 280px; }
  .trip-date-popup .ant-picker-date-panel .ant-picker-content { width: 252px; }
  .trip-date-popup .ant-picker-time-panel { height: 130px; border-left: 0; border-top: 1px solid #f0f0f0; }
  .trip-date-popup .ant-picker-time-panel .ant-picker-content { width: 100%; height: 100px; }
}
</style>
