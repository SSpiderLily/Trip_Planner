<template>
  <a-button @click="exportImages">📷 {{ exporting ? '正在生成图片…' : '导出行程长图' }}</a-button>
  <div v-if="snapshot" class="render-host" aria-hidden="true">
    <article ref="wholeRef" class="export-sheet">
      <header class="export-header">
        <div><small>旅行计划</small><h1>{{ snapshot.plan.planning_conditions.city }} · {{ snapshot.plan.days.length }} 天</h1><p>{{ snapshot.plan.planning_conditions.start_date }} — {{ snapshot.plan.planning_conditions.end_date }}</p></div>
        <div class="export-total"><small>已知费用小计 / 人</small><strong>¥{{ snapshot.plan.cost_summary.known_total }}</strong><small v-if="!snapshot.plan.cost_summary.complete">部分费用未查询到</small></div>
      </header>
      <ExportTripContext :plan="snapshot.plan" />
      <section v-for="day in snapshot.plan.days" :key="`whole-${day.date}`" class="export-day">
        <DayExportContent :day="day" :status="snapshot.statuses[day.date] || 'idle'" :day-number="dayNumber(day.date)" />
      </section>
    </article>
    <article v-for="day in snapshot.plan.days" :key="`page-${day.date}`" :ref="el => setDayRef(day.date, el)" class="export-sheet export-single-day">
      <header class="export-header compact"><div><small>旅行计划 · 第 {{ dayNumber(day.date) }} 天</small><h1>{{ snapshot.plan.planning_conditions.city }}</h1><p>{{ day.date }}</p></div></header>
      <ExportTripContext :plan="snapshot.plan" />
      <DayExportContent :day="day" :status="snapshot.statuses[day.date] || 'idle'" :day-number="dayNumber(day.date)" />
    </article>
  </div>
</template>

<script setup lang="ts">
import { defineComponent, h, nextTick, ref } from 'vue'
import { message } from 'ant-design-vue'
import html2canvas from 'html2canvas'
import type { Issue, RouteDay, RouteItinerary } from '@/types/itinerary'

const props = defineProps<{ plan: RouteItinerary; statuses: Record<string, string> }>()
const exporting = ref(false)
const snapshot = ref<{ plan: RouteItinerary; statuses: Record<string, string> } | null>(null)
const wholeRef = ref<HTMLElement | null>(null)
const dayRefs = new Map<string, HTMLElement>()
const MAX_HEIGHT = 12000
const dayNumber = (date: string) => Math.max(1, (snapshot.value?.plan.days || props.plan.days).findIndex(day => day.date === date) + 1)
const modeLabel = (mode: string) => ({ walking: '步行', bicycling: '骑行', cycling: '骑行', transit: '公共交通', driving: '驾车' } as Record<string, string>)[mode] || mode
const periodLabel = (period: string, startAt?: string | null) => {
  const hour = startAt ? Number(startAt.match(/(?:T|\s)?(\d{2}):\d{2}/)?.[1]) : Number.NaN
  if (Number.isFinite(hour)) return hour < 11 ? '上午' : hour < 14 ? '午餐' : hour < 18 ? '下午' : '晚间'
  return ({ morning: '上午', lunch: '午餐', afternoon: '下午', dinner: '晚餐', evening: '晚间' } as Record<string, string>)[period] || period
}
const formatDateTime = (value: string) => value ? value.replace('T', ' ').replace(/([+-]\d\d:\d\d|Z)$/, '') : '未提供'
const costUnitLabel = (value?: string | null) => {
  if (!value || value === 'reference') return ''
  return ({ room_night: '每房每晚', per_person: '每人', person: '每人' } as Record<string, string>)[value] || value
}
function setDayRef(date: string, element: unknown) {
  if (element instanceof HTMLElement) dayRefs.set(date, element)
}

const ExportTripContext = defineComponent({
  props: { plan: { type: Object as () => RouteItinerary, required: true } },
  setup(contextProps) {
    return () => h('section', { class: 'export-trip-context' }, [
      h('div', [h('b', contextProps.plan.lodging_base.user_input ? '已定住处' : '推荐住宿区域'), h('p', contextProps.plan.lodging_base.user_input || contextProps.plan.lodging_base.area_name || '未提供住宿信息')]),
      h('div', [h('b', '到达与离开'), h('p', `到达 ${formatDateTime(contextProps.plan.planning_conditions.arrival_at)}${contextProps.plan.planning_conditions.arrival_place ? ` · ${contextProps.plan.planning_conditions.arrival_place.name}` : ''}`), h('p', `离开 ${formatDateTime(contextProps.plan.planning_conditions.departure_at)}${contextProps.plan.planning_conditions.departure_place ? ` · ${contextProps.plan.planning_conditions.departure_place.name}` : ''}`)]),
      contextProps.plan.issues.some(issue => !issue.date) ? h('ul', { class: 'export-global-issues' }, contextProps.plan.issues.filter((issue: Issue) => !issue.date).map((issue: Issue) => h('li', { key: issue.issue_id }, issue.message))) : null
    ])
  }
})

const DayExportContent = defineComponent({
  props: { day: { type: Object as () => RouteDay, required: true }, status: { type: String, required: true }, dayNumber: { type: Number, required: true } },
  setup(dayProps) {
    return () => h('div', { class: 'export-content' }, [
      h('div', { class: 'export-day-heading' }, [
        h('div', [h('small', `第 ${dayProps.dayNumber} 天`), h('h2', dayProps.day.date), h('p', dayProps.day.description || '当天暂无景点')]),
        h('div', { class: 'export-status' }, dayProps.status === 'updating' ? '路线更新中' : dayProps.status === 'failed' ? '路线更新失败 · 以下为当前页面内容' : '')
      ]),
      dayProps.day.issues?.length ? h('ul', { class: 'export-issues' }, dayProps.day.issues.map(issue => h('li', { key: issue.issue_id }, issue.message))) : null,
      dayProps.status === 'updating' ? h('p', { class: 'export-note' }, '路线和时间正在重新计算。') : null,
      dayProps.status === 'failed' ? h('p', { class: 'export-note' }, '路线更新未完成，保留了当前景点列表。') : null,
      h('div', { class: 'export-summary' }, [
        dayProps.status === 'idle' ? `已知安排 ${dayProps.day.time_summary.known_minutes ?? '—'} 分钟` : '路线时间待更新',
        dayProps.status === 'idle' ? `费用小计 ¥${dayProps.day.cost_summary.known_total}${dayProps.day.cost_summary.complete ? '' : ' · 部分未查询到'}` : '本日费用小计待更新'
      ].map(text => h('span', text))),
      dayProps.day.weather ? h('p', { class: 'export-weather' }, `天气参考：${dayProps.day.weather.dayweather} · ${dayProps.day.weather.nighttemp}–${dayProps.day.weather.daytemp}℃`) : h('p', { class: 'export-weather' }, '未查询到当天预报'),
      dayProps.status === 'idle' && dayProps.day.legs.length ? h('div', { class: 'export-legs' }, [
        h('b', '路段交通参考'),
        ...dayProps.day.legs.map(leg => {
          const chosen = leg.selected_mode || leg.mode
          const options = Object.values(leg.options || {}).filter(option => !!option)
          const optionText = options.length ? options.map(option => `${modeLabel(option.mode)} ${option.duration_minutes == null ? '暂无耗时数据' : `约 ${option.duration_minutes} 分钟`}${option.mode === chosen ? '（当前采用）' : ''}${option.mode === leg.fastest_mode ? '（最快）' : ''}`).join('；') : '未查询到可用耗时'
          return h('div', { class: 'export-leg-row', key: leg.leg_id }, `${leg.origin?.name || '起点'} → ${leg.destination?.name || '终点'}：${optionText}`)
        })
      ]) : dayProps.status !== 'idle' ? h('p', { class: 'export-note' }, '更新期间不展示旧路线耗时。') : null,
      dayProps.day.activities.length ? dayProps.day.activities.map((activity, index) => {
        return h('section', { class: 'export-activity', key: activity.activity_id }, [
          h('div', { class: 'export-activity-main' }, [
            h('b', `${index + 1}. ${activity.title}`),
            h('span', activity.duration_minutes == null ? '建议时长未查询到' : `建议游玩约 ${activity.duration_minutes} 分钟`)
          ]),
          h('small', dayProps.status !== 'idle' ? '时段与时刻待更新' : `${periodLabel(activity.period, activity.start_at)}${activity.start_at || activity.end_at ? ` · ${[activity.start_at, activity.end_at].filter(Boolean).map(value => value!.slice(11, 16)).join('–')}` : ''}`),
          activity.place ? h('p', `${activity.place.name}${activity.place.address ? ` · ${activity.place.address}` : ''}`) : null,
          h('p', { class: 'export-opening' }, `开放时间：${activity.opening_hours || '未查询到'}`),
          activity.description ? h('p', { class: 'export-description' }, activity.description) : null,
          activity.photos?.length ? h('div', { class: 'export-photo-strip' }, activity.photos.slice(0, 3).map(photo => h('img', {
            src: photo, alt: `${activity.title}地点图片`,
            onError: (event: Event) => {
              const target = event.currentTarget as HTMLImageElement
              const placeholder = document.createElement('span')
              placeholder.className = 'export-photo-failed'
              placeholder.textContent = '图片加载失败'
              target.replaceWith(placeholder)
            }
          }))) : null,
          h('small', activity.reference_cost?.amount == null ? '未查询到真实数据' : `参考 ¥${activity.reference_cost.amount}${costUnitLabel(activity.reference_cost.unit || activity.reference_cost.basis) ? ` · ${costUnitLabel(activity.reference_cost.unit || activity.reference_cost.basis)}` : ''}`)
        ])
      }) : h('div', { class: 'export-empty' }, '当天暂无景点，可从地图添加。')
    ])
  }
})

async function renderElement(element: HTMLElement): Promise<Blob> {
  await Promise.all([...element.querySelectorAll('img')].map(image => image.complete ? Promise.resolve() : new Promise<void>(resolve => {
    image.addEventListener('load', () => resolve(), { once: true })
    image.addEventListener('error', () => resolve(), { once: true })
    setTimeout(resolve, 3000)
  })))
  const canvas = await html2canvas(element, { backgroundColor: '#fffdf8', scale: 2, useCORS: true, logging: false, windowWidth: 1120 })
  return await new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('无法生成图片')), 'image/png'))
}
function textChunks(value: string, size = 1200) {
  const parts: string[] = []
  let remaining = value
  while (remaining.length > size) {
    let end = remaining.lastIndexOf('\n', size)
    if (end < Math.floor(size * 0.55)) end = remaining.lastIndexOf(' ', size)
    if (end < Math.floor(size * 0.55)) end = size
    parts.push(remaining.slice(0, end))
    remaining = remaining.slice(end)
  }
  if (remaining) parts.push(remaining)
  return parts.length ? parts : ['']
}
function splitTextTarget(block: HTMLElement): HTMLElement | null {
  const candidates = [...block.querySelectorAll<HTMLElement>('.export-description, p, li, .export-leg-row, h1, h2, h3, b, strong, span')]
  candidates.sort((left, right) => (right.textContent?.length || 0) - (left.textContent?.length || 0))
  return candidates[0] || (block.matches('p, li, .export-leg-row, h1, h2, h3, b, strong, span') ? block : null)
}
function splitOversizedBlock(block: HTMLElement): HTMLElement[] {
  const textContainer = splitTextTarget(block)
  if (!textContainer || block.scrollHeight <= MAX_HEIGHT - 2500) return [block.cloneNode(true) as HTMLElement]
  const value = textContainer.textContent || ''
  const size = value.length < 400 ? Math.max(1, Math.ceil(value.length / 2)) : 400
  const chunks = textChunks(value, size)
  if (chunks.join('') !== value) throw new Error('长文本分页校验失败，未生成不完整图片。')
  return chunks.map((part, index) => {
    if (index === 0) {
      const clone = block.cloneNode(true) as HTMLElement
      const target = splitTextTarget(clone)
      if (target) target.textContent = part
      return clone
    }
    const continuation = textContainer.cloneNode(true) as HTMLElement
    continuation.textContent = part
    continuation.classList.add('export-continuation')
    return continuation
  })
}
function splitDayByMeasuredBlocks(element: HTMLElement, depth = 0): HTMLElement[] {
  if (depth > 8) throw new Error('行程内容过长，图片无法在安全尺寸内完整分页。')
  const content = element.querySelector<HTMLElement>('.export-content')
  const header = element.querySelector<HTMLElement>('.export-header')
  const tripContext = element.querySelector<HTMLElement>('.export-trip-context')
  if (!content) return [element]
  const intro = content.querySelector<HTMLElement>('.export-day-heading')
  const introDescription = intro?.querySelector<HTMLElement>('p')
  const globalContextBlocks = tripContext ? [...tripContext.querySelectorAll<HTMLElement>(':scope > div > *, :scope > .export-global-issues > li')] : []
  const dayIssueBlocks = [...content.querySelectorAll<HTMLElement>('.export-issues > li')]
  const routeRows = [...content.querySelectorAll<HTMLElement>('.export-leg-row')]
  const activities = [...content.querySelectorAll<HTMLElement>('.export-activity, .export-empty')]
  const informationalBlocks = [...content.querySelectorAll<HTMLElement>('.export-note, .export-summary > span, .export-weather')]
  const routeHeading = content.querySelector<HTMLElement>('.export-legs > b')
  const flowBlocks = [...globalContextBlocks, ...(introDescription ? [introDescription] : []), ...dayIssueBlocks, ...informationalBlocks, ...(routeRows.length && routeHeading ? [routeHeading] : []), ...routeRows, ...activities]
  const pages: HTMLElement[] = []
  let current = document.createElement('article')
  let currentContent = document.createElement('div')
  let hasFlow = false
  const startPage = () => {
    const page = document.createElement('article')
    page.className = 'export-sheet export-slice'
    if (header) page.append(header.cloneNode(true))
    currentContent = document.createElement('div')
    currentContent.className = 'export-content'
    if (intro) {
      const pageIntro = intro.cloneNode(true) as HTMLElement
      pageIntro.querySelector('p')?.remove()
      currentContent.append(pageIntro)
    }
    page.append(currentContent)
    document.body.append(page)
    current = page
    hasFlow = false
  }
  const finishPage = () => { if (current) pages.push(current) }
  startPage()
  for (const original of flowBlocks) {
    for (const piece of splitOversizedBlock(original)) {
      currentContent.append(piece)
      if (current.scrollHeight > MAX_HEIGHT && hasFlow) {
        piece.remove()
        finishPage()
        startPage()
        currentContent.append(piece)
      }
      hasFlow = true
    }
  }
  if (!flowBlocks.length || hasFlow) finishPage()
  if (!pages.length) {
    startPage()
    finishPage()
  }
  if (pages.some(page => page.scrollHeight > MAX_HEIGHT)) throw new Error('行程图片分页超过浏览器安全尺寸。')
  return pages
}
function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
async function exportImages() {
  if (exporting.value) return
  exporting.value = true
  const frozen = JSON.parse(JSON.stringify({ plan: props.plan, statuses: props.statuses })) as { plan: RouteItinerary; statuses: Record<string, string> }
  snapshot.value = frozen
  try {
    await nextTick()
    await document.fonts?.ready
    const whole = wholeRef.value
    if (!whole) throw new Error('行程图片区域未准备好')
    const wholeHeight = whole.scrollHeight
    const pages = wholeHeight <= MAX_HEIGHT
      ? [whole]
      : [...frozen.plan.days].flatMap(day => {
          const element = dayRefs.get(day.date)
          if (!element) return []
          if (element.scrollHeight <= MAX_HEIGHT) return [element]
          return splitDayByMeasuredBlocks(element)
        })
    message.info(`本次将生成 ${pages.length} 张行程图片，正在开始导出。`)
    const filePrefix = `旅行计划_${frozen.plan.planning_conditions.city}`
    for (const [index, page] of pages.entries()) {
      const blob = await renderElement(page)
      download(blob, `${filePrefix}_${index + 1}.png`)
      await new Promise(resolve => setTimeout(resolve, 350))
    }
    message.success(`已生成 ${pages.length} 张行程图片`)
  } catch (error: any) {
    message.error(error?.message || '图片生成失败，请重试')
  } finally {
    document.querySelectorAll('.export-slice').forEach(element => element.remove())
    snapshot.value = null
    exporting.value = false
  }
}
</script>

<style>
.render-host { position: fixed; top: 0; left: -12000px; width: 1080px; z-index: -1; pointer-events: none; }
.export-sheet { box-sizing: border-box; width: 1080px; padding: 48px; color: #203a35; background: #fffdf8; font-family: system-ui, sans-serif; }
.export-header { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #dce9e1; padding-bottom: 26px; margin-bottom: 24px; }
.export-header h1 { margin: 8px 0; font-size: 38px; }.export-header p { color: #667c75; }
.export-total { display: grid; text-align: right; gap: 5px; }.export-total strong { font-size: 30px; }
.export-trip-context { display: grid; grid-template-columns: 1fr 1.5fr; gap: 16px; margin: 12px 0 24px; padding: 16px; background: #f0f5f2; border-radius: 10px; font-size: 13px; }
.export-trip-context > * { min-width: 0; overflow-wrap: anywhere; }.export-trip-context p { margin: 5px 0; overflow-wrap: anywhere; }.export-global-issues { grid-column: 1 / -1; margin: 0; padding: 10px 28px; color: #965139; background: #fff5ec; border-radius: 8px; overflow-wrap: anywhere; }
.export-day { margin-bottom: 34px; break-inside: avoid; }.export-day-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.export-day-heading h2 { margin: 5px 0; font-size: 25px; }.export-day-heading p { color: #667c75; }
.export-status { font-size: 13px; color: #a65537; }.export-summary { display: flex; gap: 12px; margin: 14px 0; }
.export-summary span { padding: 9px 12px; background: #edf4ef; border-radius: 8px; font-size: 13px; }
.export-issues { padding: 10px 30px; color: #994c34; background: #fff5ec; border-radius: 8px; overflow-wrap: anywhere; }
.export-note { color: #994c34; }.export-activity { margin: 12px 0; padding: 17px 18px; border: 1px solid #dce7e0; border-radius: 12px; break-inside: avoid; }
.export-leg { margin-bottom: 10px; color: #477b6e; font-size: 13px; }.export-activity-main { display: flex; justify-content: space-between; gap: 16px; }.export-activity-main b,.export-activity-main span { min-width: 0; overflow-wrap: anywhere; }
.export-legs { margin: 12px 0; padding: 12px; background: #f3f7f4; border-radius: 10px; font-size: 12px; }.export-leg-row { padding: 6px 0; line-height: 1.6; overflow-wrap: anywhere; }
.export-weather { color: #667c75; font-size: 13px; }.export-slice { position: fixed; top: 0; left: -12000px; }
.export-activity p { margin: 8px 0; line-height: 1.6; overflow-wrap: anywhere; }.export-activity small { color: #667c75; overflow-wrap: anywhere; }
.export-opening { color: #667c75; font-size: 12px; }.export-photo-strip { display: flex; gap: 8px; margin: 8px 0; }.export-photo-strip img,.export-photo-failed { width: 140px; height: 88px; object-fit: cover; border-radius: 7px; background: #edf2ee; }.export-photo-failed { display: grid; place-items: center; color: #77847e; font-size: 11px; }
.export-empty { padding: 24px; text-align: center; color: #667c75; background: #f2f6f3; border-radius: 10px; }
</style>
