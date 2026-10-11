<template>
  <main class="plan-page">
    <section class="plan-heading">
      <span class="eyebrow">PLAN YOUR JOURNEY</span>
      <h1>把想去的地方，<em>慢慢排进日子里。</em></h1>
      <p>填写目的地、到离时间和你在意的事。提交后会进入行程预览，规划过程仍在后台继续。</p>
      <p v-if="hasCurrentPreview" class="continue-current"><router-link to="/preview">继续上次预览 <span aria-hidden="true">↗</span></router-link></p>
    </section>

    <section class="plan-card" aria-labelledby="plan-form-title">
      <div class="card-heading">
        <div><span class="section-index">01 / TRIP DETAILS</span><h2 id="plan-form-title">这趟旅行的基本信息</h2></div>
        <span class="required-note"><i aria-hidden="true">*</i> 必填</span>
      </div>

      <div v-if="acceptedAccess" class="accepted-panel" role="status" aria-live="polite">
        <span class="accepted-mark" aria-hidden="true">✓</span>
        <div class="accepted-copy">
          <strong>规划任务已接收</strong>
          <span v-if="accessSaved">访问信息已保存在本机。你可以继续打开预览。</span>
          <span v-else>本机暂时无法保存访问信息。当前页面仍保留本次访问信息；关闭或刷新页面后可能无法恢复。</span>
        </div>
        <div class="accepted-actions">
          <button v-if="!accessSaved" class="secondary-button" type="button" :disabled="savingAccess" @click="retrySaveAccess">
            {{ savingAccess ? '正在保存…' : '重试保存' }}
          </button>
          <button class="primary-button" type="button" @click="continueToPreview">继续查看预览 <span aria-hidden="true">↗</span></button>
        </div>
      </div>

      <form v-else class="plan-form" @submit.prevent="handleSubmit">
        <div class="form-section">
          <span class="section-index">01</span>
          <div class="section-content">
            <h3>目的地与时间</h3>
            <div class="field-grid">
              <label class="field full-field">
                <span>目的地城市 <i>*</i></span>
                <a-input v-model:value="form.city" size="large" placeholder="例如：上海、成都" autocomplete="address-level2" />
              </label>
              <label class="field">
                <span>到达时间 <i>*</i></span>
                <a-date-picker
                  v-model:value="form.arrivalAt"
                  format="YYYY-MM-DD HH:mm"
                  :show-time="{ format: 'HH:mm' }"
                  placeholder="选择到达日期和时间"
                  size="large"
                  class="full-width"
                  popup-class-name="trip-date-popup"
                />
              </label>
              <label class="field">
                <span>离开时间 <i>*</i></span>
                <a-date-picker
                  v-model:value="form.departureAt"
                  format="YYYY-MM-DD HH:mm"
                  :show-time="{ format: 'HH:mm' }"
                  placeholder="选择离开日期和时间"
                  size="large"
                  class="full-width"
                  popup-class-name="trip-date-popup"
                />
              </label>
            </div>
            <p class="field-help">日期和时间按中国标准时间提交。</p>
          </div>
        </div>

        <div class="form-section">
          <span class="section-index">02</span>
          <div class="section-content">
            <h3>预算与偏好</h3>
            <div class="field-grid">
              <label class="field">
                <span>全程预算 <small>选填，人民币</small></span>
                <a-input-number v-model:value="form.budgetAmount" :min="0" :precision="0" size="large" placeholder="例如：5000" class="full-width" />
              </label>
              <label class="field">
                <span>已订住处 <small>选填</small></span>
                <a-input v-model:value="form.lodgingQuery" size="large" :maxlength="300" placeholder="酒店名称或具体地址" />
              </label>
            </div>
            <p class="field-help">预算按全程填写，不需要填写出行人数。已订住处可填写酒店名称或地址。</p>
            <div class="field preference-field">
              <span class="field-title">旅行偏好 <small>可多选</small></span>
              <div class="preference-options" role="group" aria-label="旅行偏好">
                <button
                  v-for="option in preferenceOptions"
                  :key="option.value"
                  type="button"
                  class="preference-chip"
                  :class="{ selected: form.preferences.includes(option.value) }"
                  :aria-pressed="form.preferences.includes(option.value)"
                  @click="togglePreference(option.value)"
                >{{ option.icon }} {{ option.value }}</button>
              </div>
            </div>
          </div>
        </div>

        <div class="form-section">
          <span class="section-index">03</span>
          <div class="section-content">
            <h3>到离地点与返程预留</h3>
            <div class="field-grid">
              <label class="field">
                <span>到达地点 <small>选填</small></span>
                <a-input v-model:value="form.arrivalPlaceQuery" size="large" :maxlength="300" placeholder="机场、车站或地址" />
              </label>
              <label class="field">
                <span>离开地点 <small>选填</small></span>
                <a-input v-model:value="form.departurePlaceQuery" size="large" :maxlength="300" placeholder="机场、车站或地址" />
              </label>
            </div>
            <p class="field-help">请填写机场、车站或其他地点名称；行程安排时会核实。</p>
            <div class="field buffer-field">
              <span class="field-title">返程预留时间 <small>选填</small></span>
              <div class="buffer-inputs">
                <a-input-number v-model:value="form.returnBufferHours" :min="0" :precision="0" size="large" placeholder="小时" />
                <span>小时</span>
                <a-input-number v-model:value="form.returnBufferMinutes" :min="0" :max="59" :precision="0" size="large" placeholder="分钟" />
                <span>分钟</span>
              </div>
              <p class="field-help">留空时默认预留 1 小时；填写 0 小时 0 分钟表示不额外预留。返程交通也会纳入行程安排。</p>
            </div>
          </div>
        </div>

        <div class="form-section last-section">
          <span class="section-index">04</span>
          <div class="section-content">
            <h3>还有什么想法？</h3>
            <label class="field">
              <span>备注 <small>选填</small></span>
              <a-textarea v-model:value="form.remarks" :rows="4" :maxlength="4000" placeholder="写下特别想去的地方、饮食偏好或希望的旅行节奏。" />
            </label>
          </div>
        </div>

        <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
        <div class="form-footer">
          <p>提交后会先接收规划任务，再打开可继续更新的预览。</p>
          <button class="primary-button submit-button" type="submit" :disabled="submitting">
            {{ submitting ? '正在提交…' : '开始规划' }} <span aria-hidden="true">↗</span>
          </button>
        </div>
      </form>
    </section>
    <p class="plan-footnote">提交后会进入预览，你可以在这台设备继续查看。</p>
  </main>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { Dayjs } from 'dayjs'
import {
  getPendingPreviewAccess,
  getLatestPreviewAccess,
  retrySavePreviewAccess,
  savePreviewAccess,
} from '@/services/v4Preview'
import { submitV4TripTask } from '@/services/v4TripTasks'
import type { V4PreviewAccess } from '@/types/v4Preview'
import { createSingleFlightSubmitter, buildV4TripRequest } from '@/utils/v4TripRequest'

const router = useRouter()
const submitting = ref(false)
const savingAccess = ref(false)
const accessSaved = ref(false)
const acceptedAccess = ref<V4PreviewAccess | null>(null)
const hasCurrentPreview = ref(false)
const formError = ref('')

const form = reactive({
  city: '',
  arrivalAt: null as Dayjs | null,
  departureAt: null as Dayjs | null,
  budgetAmount: null as number | null,
  lodgingQuery: '',
  arrivalPlaceQuery: '',
  departurePlaceQuery: '',
  returnBufferHours: null as number | null,
  returnBufferMinutes: null as number | null,
  preferences: [] as string[],
  remarks: '',
})

const preferenceOptions = [
  { value: '历史文化', icon: '◈' },
  { value: '自然风光', icon: '✳' },
  { value: '美食', icon: '✦' },
  { value: '城市漫步', icon: '◇' },
  { value: '艺术', icon: '✺' },
  { value: '休闲', icon: '☼' },
]

const submitOnce = createSingleFlightSubmitter(submitV4TripTask)

function togglePreference(value: string): void {
  const index = form.preferences.indexOf(value)
  if (index === -1) form.preferences.push(value)
  else form.preferences.splice(index, 1)
}

function requestId(): string {
  const id = globalThis.crypto?.randomUUID?.()
  return id ? `request_${id}` : `request_${Date.now()}_${Math.random().toString(36).slice(2)}`
}

async function handleSubmit(): Promise<void> {
  if (submitting.value || acceptedAccess.value) return
  formError.value = ''
  let request
  try {
    request = buildV4TripRequest({
      city: form.city,
      arrivalLocal: form.arrivalAt?.format('YYYY-MM-DDTHH:mm') ?? null,
      departureLocal: form.departureAt?.format('YYYY-MM-DDTHH:mm') ?? null,
      budgetAmount: form.budgetAmount,
      lodgingQuery: form.lodgingQuery,
      arrivalPlaceQuery: form.arrivalPlaceQuery,
      departurePlaceQuery: form.departurePlaceQuery,
      returnBufferHours: form.returnBufferHours,
      returnBufferMinutes: form.returnBufferMinutes,
      preferences: form.preferences,
      remarks: form.remarks,
    }, requestId())
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '请检查填写内容。'
    return
  }

  submitting.value = true
  try {
    const accepted = await submitOnce(request)
    const access: V4PreviewAccess = {
      taskId: accepted.task_id,
      planId: accepted.plan_id,
      accessToken: accepted.access_token,
    }
    acceptedAccess.value = access
    try {
      savePreviewAccess(access.taskId, access.accessToken, access.planId)
      accessSaved.value = true
    } catch {
      accessSaved.value = false
      formError.value = '任务已接收，但本机暂时无法保存预览访问信息。请重试保存，或直接继续查看。'
    }
    await continueToPreview()
  } catch (error) {
    formError.value = safeSubmitError(error)
  } finally {
    submitting.value = false
  }
}

function safeSubmitError(error: unknown): string {
  const status = typeof error === 'object' && error !== null && 'response' in error
    ? (error as { response?: { status?: number } }).response?.status
    : undefined
  if (status === 422) return '填写内容未通过校验，请检查城市和到离时间。'
  if (status === 503) return '规划服务暂时不可用，你填写的内容仍保留在本页。'
  if (status) return `提交没有完成（${status}）。请检查服务状态后再试。`
  return '暂时无法连接规划服务。请检查网络后再试；本页不会自动重发请求。'
}

async function retrySaveAccess(): Promise<void> {
  const access = acceptedAccess.value
  if (!access || savingAccess.value) return
  savingAccess.value = true
  formError.value = ''
  try {
    const saved = retrySavePreviewAccess()
    if (!saved) throw new Error('access_storage_unavailable')
    accessSaved.value = true
    await continueToPreview()
  } catch {
    formError.value = '保存仍未成功。访问信息保留在当前页面内，你也可以继续查看预览。'
  } finally {
    savingAccess.value = false
  }
}

async function continueToPreview(): Promise<void> {
  try {
    await router.push('/preview')
  } catch {
    formError.value = '任务已接收。请点击“继续查看预览”再次打开。'
  }
}

onMounted(() => {
  const pending = getPendingPreviewAccess()
  if (pending) {
    acceptedAccess.value = pending
    accessSaved.value = false
    formError.value = '发现一份已接收、尚未成功保存访问信息的规划任务。可以重试保存或继续查看。'
    return
  }
  hasCurrentPreview.value = Boolean(getLatestPreviewAccess())
})

</script>

<style scoped>
.plan-page { max-width: 1120px; margin: 0 auto; padding: clamp(42px, 7vw, 88px) clamp(20px, 5vw, 64px) 76px; }
.plan-heading { max-width: 760px; margin: 0 auto 36px; text-align: center; }
.eyebrow, .section-index { color: #a65e46; font-size: 10px; font-weight: 700; letter-spacing: .2em; }
.plan-heading h1 { margin: 18px 0 13px; font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: clamp(31px, 5vw, 52px); line-height: 1.42; letter-spacing: -.06em; font-weight: 600; }
.plan-heading h1 em { color: #a85b3f; font-weight: 500; font-style: normal; }
.plan-heading p { margin: 0; color: #68766e; font-size: 14px; line-height: 1.8; }
.plan-heading .continue-current { margin-top: 12px; font-size: 13px; }
.continue-current a { color: #a65e46; font-weight: 700; text-underline-offset: 4px; }
.plan-card { padding: clamp(22px, 4vw, 48px); background: #fffdfa; border: 1px solid #e2e4da; box-shadow: 12px 12px 0 #e9eee3; }
.card-heading { display: flex; justify-content: space-between; align-items: start; gap: 20px; padding-bottom: 24px; border-bottom: 1px solid #e3e5db; }
.card-heading h2 { margin: 8px 0 0; color: #20372e; font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 24px; font-weight: 600; }
.required-note { color: #829087; font-size: 12px; white-space: nowrap; }
.required-note i, .field > span i { color: #bc6949; font-style: normal; }
.plan-form { padding-top: 8px; }
.form-section { display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 14px; padding: 27px 0; border-bottom: 1px solid #e8e9e1; }
.section-index:not(.card-heading .section-index) { display: grid; width: 30px; height: 30px; place-items: center; border-radius: 50%; background: #dce9bd; color: #365340; letter-spacing: 0; }
.section-content h3 { margin: 1px 0 18px; font-family: 'Noto Serif SC', 'Songti SC', serif; font-size: 20px; font-weight: 600; color: #243a30; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 20px; }
.field { display: flex; flex-direction: column; gap: 9px; color: #485a51; font-size: 12px; font-weight: 700; }
.field > span small, .field-title small { margin-left: 7px; color: #98a29a; font-size: 11px; font-weight: 400; }
.full-field { grid-column: 1 / -1; }
.full-width { width: 100%; }
.field-help { margin: 10px 0 0; color: #88958c; font-size: 11px; line-height: 1.7; font-weight: 400; }
.preference-field { margin-top: 22px; }
.field-title { color: #485a51; font-size: 12px; font-weight: 700; }
.preference-options { display: flex; flex-wrap: wrap; gap: 9px; }
.preference-chip { border: 1px solid #d9dfd5; background: #fffdfa; color: #58665c; padding: 9px 12px; border-radius: 3px; font-size: 12px; cursor: pointer; transition: background .2s, color .2s, border .2s; }
.preference-chip:hover { border-color: #658165; }
.preference-chip.selected { background: #213e32; border-color: #213e32; color: #fffdfa; }
.buffer-field { margin-top: 22px; }
.buffer-inputs { display: flex; align-items: center; gap: 9px; }
.buffer-inputs .ant-input-number { width: 126px; }
.buffer-inputs > span { color: #68766e; font-size: 12px; font-weight: 400; }
.last-section { border-bottom: 0; }
.form-error { margin: 6px 0 0; padding: 13px 16px; border-left: 3px solid #b96545; background: #fbf0e9; color: #704938; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.form-footer { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding-top: 23px; border-top: 1px solid #e3e5db; }
.form-footer p { margin: 0; color: #88958c; font-size: 11px; line-height: 1.7; }
.primary-button, .secondary-button { min-height: 44px; padding: 0 19px; border: 0; border-radius: 24px; font-size: 12px; font-weight: 700; cursor: pointer; }
.primary-button { background: #1d392e; color: #fffdfa; }
.primary-button:hover:not(:disabled) { background: #315e49; }
.primary-button:disabled { cursor: wait; opacity: .65; }
.primary-button span { margin-left: 15px; color: #dce9bd; font-size: 17px; }
.submit-button { min-width: 176px; }
.secondary-button { border: 1px solid #d6ddd2; background: transparent; color: #345b49; }
.accepted-panel { display: flex; align-items: center; gap: 16px; padding: 25px 0 0; }
.accepted-mark { display: grid; width: 38px; height: 38px; flex: none; place-items: center; border-radius: 50%; background: #dce9bd; color: #35553b; font-size: 19px; }
.accepted-copy { display: grid; flex: 1; gap: 5px; color: #627168; font-size: 12px; line-height: 1.65; }
.accepted-copy strong { color: #23392e; font-size: 15px; }
.accepted-actions { display: flex; flex-wrap: wrap; justify-content: end; gap: 9px; }
.plan-footnote { max-width: 760px; margin: 28px auto 0; color: #89948b; font-size: 11px; line-height: 1.7; text-align: center; }
:deep(.ant-input), :deep(.ant-picker), :deep(.ant-input-number) { min-height: 46px; border-color: #d8ded4; border-radius: 3px; box-shadow: none; background: #fffdfa; }
:deep(.ant-input-number-input) { height: 44px; }
:deep(.ant-picker) { display: flex; align-items: center; }
:deep(textarea.ant-input) { min-height: 110px; resize: vertical; }
@media (max-width: 620px) {
  .plan-page { padding: 36px 18px 56px; }
  .plan-heading { margin-bottom: 26px; text-align: left; }
  .plan-heading h1 { font-size: 32px; }
  .plan-card { padding: 22px 18px; box-shadow: 7px 7px 0 #e9eee3; }
  .card-heading h2 { font-size: 20px; }
  .form-section { grid-template-columns: 31px minmax(0, 1fr); gap: 10px; padding: 23px 0; }
  .field-grid { grid-template-columns: 1fr; gap: 16px; }
  .full-field { grid-column: auto; }
  .form-footer { align-items: stretch; flex-direction: column; }
  .submit-button { width: 100%; }
  .accepted-panel { align-items: flex-start; flex-wrap: wrap; }
  .accepted-copy { flex-basis: calc(100% - 55px); }
  .accepted-actions { width: 100%; justify-content: stretch; }
  .accepted-actions button { flex: 1; }
  .buffer-inputs .ant-input-number { width: min(115px, 30vw); }
}
</style>
