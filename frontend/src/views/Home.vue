<template>
  <div class="home-container">
    <!-- 背景装饰 -->
    <div class="bg-decoration">
      <div class="circle circle-1"></div>
      <div class="circle circle-2"></div>
      <div class="circle circle-3"></div>
    </div>

    <!-- 页面标题 -->
    <div class="page-header">
      <div class="icon-wrapper">
        <span class="icon">✈️</span>
      </div>
      <h1 class="page-title">智能旅行助手</h1>
      <p class="page-subtitle">去哪玩、吃什么、怎么走，一次安排清楚</p>
    </div>

    <a-card class="form-card" :bordered="false">
      <a-form
        :model="formData"
        layout="vertical"
        @finish="handleSubmit"
      >
        <!-- 第一步:目的地和日期 -->
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">📍</span>
            <span class="section-title">目的地与日期</span>
          </div>

          <a-row :gutter="24">
            <a-col :span="6">
              <a-form-item name="city" :rules="[{ required: true, message: '请输入目的地城市' }]">
                <template #label>
                  <span class="form-label">目的地城市</span>
                </template>
                <a-input
                  v-model:value="formData.city"
                  placeholder="例如: 北京"
                  size="large"
                  class="custom-input"
                >
                  <template #prefix>
                    <span style="color: #1890ff;">🏙️</span>
                  </template>
                </a-input>
              </a-form-item>
            </a-col>
            <a-col :span="7">
              <a-form-item name="arrival_at" :rules="[{ required: true, message: '请选择到达日期和时间' }]">
                <template #label>
                  <span class="form-label">到达时间</span>
                </template>
                <a-date-picker
                  v-model:value="formData.arrival_at"
                  show-time
                  format="YYYY-MM-DD HH:mm"
                  style="width: 100%"
                  size="large"
                  class="custom-input"
                  placeholder="选择到达日期和时间"
                />
              </a-form-item>
            </a-col>
            <a-col :span="7">
              <a-form-item name="departure_at" :rules="[{ required: true, message: '请选择离开日期和时间' }]">
                <template #label>
                  <span class="form-label">离开时间</span>
                </template>
                <a-date-picker
                  v-model:value="formData.departure_at"
                  show-time
                  format="YYYY-MM-DD HH:mm"
                  style="width: 100%"
                  size="large"
                  class="custom-input"
                  placeholder="选择离开日期和时间"
                />
              </a-form-item>
            </a-col>
            <a-col :span="4">
              <a-form-item>
                <template #label>
                  <span class="form-label">旅行天数</span>
                </template>
                <div class="days-display-compact">
                  <span class="days-value">{{ formData.travel_days }}</span>
                  <span class="days-unit">天</span>
                </div>
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <div class="endpoint-fields">
          <a-form-item label="到达地点（选填）" extra="例如机场、火车站或其他地点；不填写时不计算该端接驳。">
            <a-auto-complete
              v-model:value="endpointText.arrival"
              :options="arrivalOptions"
              :disabled="!formData.city.trim()"
              placeholder="输入地点名称并从目的地搜索结果中选择"
              @search="searchArrivalEndpoint"
              @select="selectArrivalEndpoint"
              @change="endpointChanged('arrival', $event)"
            />
          </a-form-item>
          <a-form-item label="离开地点（选填）" extra="未填写时不计算该端接驳。">
            <a-auto-complete
              v-model:value="endpointText.departure"
              :options="departureOptions"
              :disabled="!formData.city.trim()"
              placeholder="输入地点名称并从目的地搜索结果中选择"
              @search="searchDepartureEndpoint"
              @select="selectDepartureEndpoint"
              @change="endpointChanged('departure', $event)"
            />
          </a-form-item>
        </div>
        <p class="defaults">默认适中节奏、公共交通结合步行；未填写住处时推荐住宿区域。</p>
        <a-collapse ghost style="margin-bottom: 24px">
          <a-collapse-panel key="preferences" header="更多偏好（选填）">
            <a-form-item label="人均参考预算（元，全程）" name="budget_per_person">
              <a-input-number v-model:value="formData.budget_per_person" :min="0" :precision="0" style="width: 100%" placeholder="可留空" />
              <small>参考预算包含独住住宿、餐饮和门票，不含交通；只统计查询到的价格。</small>
            </a-form-item>
            <a-form-item label="已定住处" name="lodging">
              <a-input v-model:value="formData.lodging" placeholder="酒店或具体地址；未确定可留空" :maxlength="300" />
            </a-form-item>
            <a-form-item label="游玩偏好" name="preferences">
              <a-checkbox-group v-model:value="formData.preferences" :options="['历史文化', '自然风光', '美食', '城市漫步', '艺术', '休闲']" />
            </a-form-item>
            <a-form-item label="其他要求" name="free_text_input">
              <a-textarea v-model:value="formData.free_text_input" placeholder="例如必去地点、自驾或少走路要求。备注由模型理解。" :rows="3" />
              <small>带老人、孩子、少走路等当前只提供提醒，尚不自动调整路线。</small>
            </a-form-item>
          </a-collapse-panel>
        </a-collapse>

        <!-- 提交按钮 -->
        <a-form-item>
          <a-button
            type="primary"
            html-type="submit"
            :loading="loading"
            size="large"
            block
            class="submit-button"
          >
            <template v-if="!loading">
              <span class="button-icon">🚀</span>
              <span>开始规划我的旅行</span>
            </template>
            <template v-else>
              <span>正在生成中...</span>
            </template>
          </a-button>
        </a-form-item>

        <!-- 加载进度条 -->
        <a-form-item v-if="loading">
          <div class="loading-container">
            <a-spin />
            <p class="loading-status">
              {{ loadingStatus }}
            </p>
          </div>
        </a-form-item>
        <p v-if="activeTaskId">任务：{{ activeTaskId }}</p>
        <router-link :to="{ path: '/observability', query: activeTaskId ? { task: activeTaskId } : {} }">查看任务、工程指标与调用记录 →</router-link>
      </a-form>
    </a-card>
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
      await router.push('/result')
      return
    }
    if (task.status === 'failed' || task.status === 'interrupted') {
      loadingStatus.value = task.persistence_error?.message || task.error_message || '任务未完成'
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
      message.warning('任务不存在或已清理，请重新提交')
      return
    }
  }
  if (!disposed) timer = setTimeout(poll, 2000)
}

const handleSubmit = async () => {
  if (loading.value) return
  if (!formData.arrival_at || !formData.departure_at) {
    message.error('请选择到达和离开日期时间')
    return
  }
  if (formData.departure_at.isBefore(formData.arrival_at)) {
    message.error('离开时间必须晚于到达时间')
    return
  }
  if (!formData.departure_at.isAfter(formData.arrival_at)) {
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
    message.error(errorText(error))
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
</script>

<style scoped>
.home-container {
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 60px 20px;
  position: relative;
  overflow: hidden;
}

/* 背景装饰 */
.bg-decoration {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: hidden;
}

.circle {
  position: absolute;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.1);
  animation: float 20s infinite ease-in-out;
}

.circle-1 {
  width: 300px;
  height: 300px;
  top: -100px;
  left: -100px;
  animation-delay: 0s;
}

.circle-2 {
  width: 200px;
  height: 200px;
  top: 50%;
  right: -50px;
  animation-delay: 5s;
}

.circle-3 {
  width: 150px;
  height: 150px;
  bottom: -50px;
  left: 30%;
  animation-delay: 10s;
}

@keyframes float {
  0%, 100% {
    transform: translateY(0) rotate(0deg);
  }
  50% {
    transform: translateY(-30px) rotate(180deg);
  }
}

/* 页面标题 */
.page-header {
  text-align: center;
  margin-bottom: 50px;
  animation: fadeInDown 0.8s ease-out;
  position: relative;
  z-index: 1;
}

.icon-wrapper {
  margin-bottom: 20px;
}

.icon {
  font-size: 80px;
  display: inline-block;
  animation: bounce 2s infinite;
}

@keyframes bounce {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-20px);
  }
}

.page-title {
  font-size: 56px;
  font-weight: 800;
  color: #ffffff;
  margin-bottom: 16px;
  text-shadow: 3px 3px 6px rgba(0, 0, 0, 0.3);
  letter-spacing: 2px;
}

.page-subtitle {
  font-size: 20px;
  color: rgba(255, 255, 255, 0.95);
  margin: 0;
  font-weight: 300;
}

/* 表单卡片 */
.form-card {
  max-width: 1400px;
  margin: 0 auto;
  border-radius: 24px;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.4);
  animation: fadeInUp 0.8s ease-out;
  position: relative;
  z-index: 1;
  backdrop-filter: blur(10px);
  background: rgba(255, 255, 255, 0.98) !important;
}

/* 表单分区 */
.form-section {
  margin-bottom: 32px;
  padding: 24px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 16px;
  border: 1px solid #e8e8e8;
  transition: all 0.3s ease;
}

.form-section:hover {
  box-shadow: 0 8px 24px rgba(102, 126, 234, 0.15);
  transform: translateY(-2px);
}

.section-header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 2px solid #667eea;
}

.section-icon {
  font-size: 24px;
  margin-right: 12px;
}

.section-title {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

.endpoint-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  margin: 0 24px 12px;
}

.endpoint-fields small {
  color: #77847f;
}

/* 表单标签 */
.form-label {
  font-size: 15px;
  font-weight: 500;
  color: #555;
}

/* 自定义输入框 */
.custom-input :deep(.ant-input),
.custom-input :deep(.ant-picker) {
  border-radius: 12px;
  border: 2px solid #e8e8e8;
  transition: all 0.3s ease;
}

.custom-input :deep(.ant-input:hover),
.custom-input :deep(.ant-picker:hover) {
  border-color: #667eea;
}

.custom-input :deep(.ant-input:focus),
.custom-input :deep(.ant-picker-focused) {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
}

/* 自定义选择框 */
.custom-select :deep(.ant-select-selector) {
  border-radius: 12px !important;
  border: 2px solid #e8e8e8 !important;
  transition: all 0.3s ease;
}

.custom-select:hover :deep(.ant-select-selector) {
  border-color: #667eea !important;
}

.custom-select :deep(.ant-select-focused .ant-select-selector) {
  border-color: #667eea !important;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1) !important;
}

/* 天数显示 - 紧凑版 */
.days-display-compact {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 40px;
  padding: 8px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 12px;
  color: white;
}

.days-display-compact .days-value {
  font-size: 24px;
  font-weight: 700;
  margin-right: 4px;
}

.days-display-compact .days-unit {
  font-size: 14px;
}

/* 偏好标签 */
.preference-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.custom-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
}

.preference-tag :deep(.ant-checkbox-wrapper) {
  margin: 0 !important;
  padding: 8px 16px;
  border: 2px solid #e8e8e8;
  border-radius: 20px;
  transition: all 0.3s ease;
  background: white;
  font-size: 14px;
}

.preference-tag :deep(.ant-checkbox-wrapper:hover) {
  border-color: #667eea;
  background: #f5f7ff;
}

.preference-tag :deep(.ant-checkbox-wrapper-checked) {
  border-color: #667eea;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

/* 自定义文本域 */
.custom-textarea :deep(.ant-input) {
  border-radius: 12px;
  border: 2px solid #e8e8e8;
  transition: all 0.3s ease;
}

.custom-textarea :deep(.ant-input:hover) {
  border-color: #667eea;
}

.custom-textarea :deep(.ant-input:focus) {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
}

/* 提交按钮 */
.submit-button {
  height: 56px;
  border-radius: 28px;
  font-size: 18px;
  font-weight: 600;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border: none;
  box-shadow: 0 8px 24px rgba(102, 126, 234, 0.4);
  transition: all 0.3s ease;
}

.submit-button:hover {
  transform: translateY(-2px);
  box-shadow: 0 12px 32px rgba(102, 126, 234, 0.5);
}

.submit-button:active {
  transform: translateY(0);
}

.button-icon {
  margin-right: 8px;
  font-size: 20px;
}

/* 加载容器 */
.loading-container {
  text-align: center;
  padding: 24px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 16px;
  border: 2px dashed #667eea;
}

.loading-status {
  margin-top: 16px;
  color: #667eea;
  font-size: 18px;
  font-weight: 500;
}

/* 动画 */
@keyframes fadeInDown {
  from {
    opacity: 0;
    transform: translateY(-30px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(30px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 720px) {
  .endpoint-fields { grid-template-columns: 1fr; gap: 0; margin: 0; }
}
</style>
