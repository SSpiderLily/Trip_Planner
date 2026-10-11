<template>
  <div class="v4-export-host" aria-hidden="true">
    <article
      v-for="(day, index) in visible.view.days"
      :key="day.id"
      :ref="(element) => setDayRef(day.id, element)"
      class="v4-export-sheet"
    >
      <header class="v4-export-header">
        <div>
          <small>旅行计划 · 第 {{ index + 1 }} 天</small>
          <h1>{{ visible.view.city }}</h1>
          <p>{{ day.date }} · {{ visible.view.arrivalAt.slice(0, 10) }} — {{ visible.view.departureAt.slice(0, 10) }}</p>
        </div>
        <div class="v4-export-cost">
          <small>已知景点与餐饮消费参考 / 人</small>
          <strong v-if="visible.view.cost.knownSubtotal !== null">¥{{ visible.view.cost.knownSubtotal }}</strong>
          <strong v-else>暂无可用金额</strong>
          <small v-if="visible.view.cost.unknownCount || visible.view.cost.conflictCount">
            {{ costGapText(visible.view.cost.unknownCount, visible.view.cost.conflictCount) }}
          </small>
          <small v-if="visible.view.tripBudget !== null">全程旅费预算 ¥{{ visible.view.tripBudget }}</small>
          <small>景点与餐饮小计不含交通；住宿消费单独展示。</small>
        </div>
      </header>

      <section v-if="index === 0 && visible.view.planIssues.length" class="v4-export-global-issues">
        <b>全程提示</b>
        <p v-for="issue in visible.view.planIssues" :key="issue.issue_id">{{ issue.message }}</p>
      </section>

      <div class="v4-export-grid">
        <main>
          <section class="v4-export-section">
            <div class="v4-export-section-title"><small>DAY {{ index + 1 }}</small><h2>{{ day.theme }}</h2></div>
            <p class="v4-export-muted">{{ day.date }} · {{ day.activities.length ? `按顺序安排 ${day.activities.length} 项活动` : '当天没有具体活动安排' }}</p>
            <div class="v4-export-facts">
              <span>已知停留 {{ formatReferenceDuration(day.timing.knownStaySeconds) }}</span>
              <span>已知交通 {{ formatReferenceDuration(day.timing.knownTravelSeconds) }}</span>
              <span v-if="day.timing.unknownLegCount">{{ day.timing.unknownLegCount }} 段交通耗时待确认</span>
              <span v-else-if="day.timing.elapsedSeconds !== null">参考安排 {{ formatReferenceDuration(day.timing.elapsedSeconds) }}</span>
              <span v-if="day.cost.knownSubtotal !== null">当日已知景点与餐饮 ¥{{ day.cost.knownSubtotal }} / 人</span>
              <span v-if="day.cost.unknownCount || day.cost.conflictCount">{{ costGapText(day.cost.unknownCount, day.cost.conflictCount) }}</span>
            </div>
          </section>

          <section class="v4-export-card">
            <b>天气参考 · {{ day.weather.known ? '旅行日预报' : '天气未知' }}</b>
            <p>{{ day.weather.summary }}</p>
            <small>{{ day.weather.advice }}</small>
          </section>

          <section class="v4-export-card v4-export-lodging">
            <div><small>{{ day.hotel.label }}</small><b>{{ day.hotel.name }}</b><p>{{ day.hotel.address || '具体酒店信息未核实' }}</p>
              <small>住宿人均消费参考：{{ day.hotel.cost.text }} · 不按房晚推算</small>
              <small v-if="day.hotel.cost.evidenceLabel">{{ day.hotel.cost.evidenceLabel }}</small>
            </div>
            <div><small>推荐寻找区域</small><b>{{ day.area.label }}</b><p>区域参考点不代表已选酒店。</p></div>
          </section>

          <section v-if="dayLevelIssues(day).length || day.unboundIssues.length" class="v4-export-issues">
            <b>当天提示</b>
            <p v-for="issue in uniqueIssues([...dayLevelIssues(day), ...day.unboundIssues])" :key="issue.issue_id">{{ issue.message }}</p>
          </section>

          <section class="v4-export-section v4-export-timeline">
            <h3>有序行程与缺项</h3>
            <p v-if="!day.timeline.length" class="v4-export-muted">当天暂无具体活动；如有缺项，会按对应时段列出。</p>
            <template v-for="item in day.timeline" :key="item.type === 'activity' ? item.activity.id : item.type === 'missing_slot' ? item.slot.id : item.issue.issue_id">
              <div v-if="item.type === 'missing_slot'" class="v4-export-missing">
                <b>{{ item.slot.periodLabel }} · {{ item.slot.kind === 'conditional' ? '待确认' : '尚未安排' }}</b>
                <p>{{ item.slot.prompt }}</p>
                <small v-for="issue in item.slot.issues" :key="issue.issue_id">{{ issue.message }}</small>
              </div>
              <div v-else-if="item.type === 'slot_issue'" class="v4-export-issue-row"><b>{{ item.periodLabel }} · {{ issueTitle(item.issue) }}</b><span>{{ item.issue.message }}</span></div>
              <article v-else class="v4-export-activity">
                <div class="v4-export-activity-head">
                  <b>{{ item.position }}. {{ item.activity.title }}</b>
                  <span>{{ item.activity.durationLabel }}</span>
                </div>
                <small>{{ item.activity.periodLabel }} · {{ item.activity.timingLabel }}</small>
                <p v-if="item.activity.address">{{ item.activity.address }}</p>
                <p v-if="item.activity.description">{{ item.activity.description }}</p>
                <div class="v4-export-cost-detail">{{ item.activity.cost.text }}</div>
                <small v-if="item.activity.cost.evidenceLabel">{{ item.activity.cost.evidenceLabel }}</small>
                <div v-for="issue in item.activity.issues" :key="issue.issue_id" class="v4-export-issue-row"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
              </article>
            </template>
          </section>

          <section v-if="day.legs.some((leg) => !leg.isReturn)" class="v4-export-section">
            <h3>交通参考</h3>
            <article v-for="leg in day.legs.filter((item) => !item.isReturn)" :key="leg.id" class="v4-export-route">
              <div class="v4-export-activity-head"><b>{{ leg.fromLabel }} → {{ leg.toLabel }}</b><span>{{ leg.selectedModeLabel }} · {{ leg.selectedDurationLabel }}</span></div>
              <div v-if="leg.modes.length" class="v4-export-modes">
                <span v-for="mode in leg.modes" :key="mode.mode" :class="{ chosen: leg.selectedMode === mode.mode }">{{ mode.label }}：{{ mode.durationLabel }}{{ leg.selectedMode === mode.mode ? '（当前采用）' : '' }}</span>
              </div>
              <small>{{ leg.geometryMessage }}</small>
              <small v-if="leg.timeBasisLabel">{{ leg.timeBasisLabel }}</small>
              <div v-for="issue in leg.issues" :key="issue.issue_id" class="v4-export-issue-row"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
            </article>
          </section>

          <section v-if="day.returnTiming.status !== 'not_applicable'" class="v4-export-return">
            <b>{{ day.returnTiming.headline }}</b>
            <p>{{ day.returnTiming.bufferLabel }}<span v-if="day.returnTiming.departureLabel"> · {{ day.returnTiming.departureLabel }}</span><span v-if="day.returnTiming.deadlineLabel"> · {{ day.returnTiming.deadlineLabel }}</span><span v-if="day.returnTiming.overrunLabel"> · {{ day.returnTiming.overrunLabel }}</span></p>
            <p v-if="day.returnTiming.detail">{{ day.returnTiming.detail }}</p>
            <article v-if="day.returnLeg" class="v4-export-route">
              <div class="v4-export-activity-head"><b>{{ day.returnLeg.fromLabel }} → {{ day.returnLeg.toLabel }}</b><span>{{ day.returnLeg.selectedModeLabel }} · {{ day.returnLeg.selectedDurationLabel }}</span></div>
              <div v-if="day.returnLeg.modes.length" class="v4-export-modes">
                <span v-for="mode in day.returnLeg.modes" :key="mode.mode" :class="{ chosen: day.returnLeg.selectedMode === mode.mode }">{{ mode.label }}：{{ mode.durationLabel }}{{ day.returnLeg.selectedMode === mode.mode ? '（当前采用）' : '' }}</span>
              </div>
              <small>{{ day.returnLeg.geometryMessage }}</small>
              <small v-if="day.returnLeg.timeBasisLabel">{{ day.returnLeg.timeBasisLabel }}</small>
              <div v-for="issue in day.returnLeg.issues" :key="issue.issue_id" class="v4-export-issue-row"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
            </article>
          </section>
        </main>

        <aside>
          <section class="v4-export-card">
            <b>当天目的地</b>
            <ol v-if="day.locations.some((location) => location.kind === 'activity')">
              <li v-for="location in day.locations.filter((item) => item.kind === 'activity')" :key="location.id">
                <span>{{ location.number }}. {{ location.name }}</span><small>{{ location.area || location.address || '区域信息未提供' }}</small>
              </li>
            </ol>
            <p v-else>当天没有可列出的具体目的地。</p>
          </section>
          <section class="v4-export-card v4-export-cost-list">
            <b>消费来源与状态</b>
            <p v-if="!day.cost.entries.length">暂无景点或餐饮消费记录。</p>
            <div v-for="entry in day.cost.entries" :key="`${entry.kind}-${entry.poi_id}-${entry.activity_ids.join('-')}`">
              <span>{{ entry.place_name }} · {{ entry.kind === 'meal' ? '餐饮' : entry.kind === 'lodging' ? '住宿' : '景点' }}</span>
              <b>{{ costEntryText(entry) }}</b>
              <small v-for="source in entry.evidence.flatMap((evidence) => evidence.sources)" :key="`${entry.poi_id}-${source.provider}-${source.queried_at}`">{{ providerLabel(source.provider) }}查询于 {{ formatPreviewEvidenceTime(source.queried_at) || '时间未提供' }}</small>
            </div>
          </section>
        </aside>
      </div>
    </article>
  </div>
</template>

<script setup lang="ts">
import { nextTick } from 'vue'
import html2canvas from 'html2canvas'
import type { ReferenceCostEntry } from '@/types/v4Preview'
import type { V4ExportFile, V4ExportVisibleState } from '@/utils/v4PreviewExportController'
import { formatPreviewEvidenceTime, formatReferenceDuration, type PreviewDayView } from '@/utils/v4PreviewMapper'

const props = defineProps<{ visible: V4ExportVisibleState }>()
const dayElements = new Map<string, HTMLElement>()
const MAX_PAGE_HEIGHT = 12000
const MAX_BLOCK_HEIGHT = 9000

function setDayRef(dayId: string, element: unknown) {
  if (element instanceof HTMLElement) dayElements.set(dayId, element)
}

function dayLevelIssues(day: PreviewDayView) {
  return day.issues.filter((issue) => issue.scope === 'day')
}

function uniqueIssues<T extends { issue_id: string }>(issues: T[]): T[] {
  return [...new Map(issues.map((issue) => [issue.issue_id, issue])).values()]
}

function issueTitle(issue: { category: string }): string {
  if (issue.category === 'known_conflict') return '可能需要调整'
  if (issue.category === 'missing_arrangement') return '安排缺项'
  if (issue.category === 'unknown_info' || issue.category === 'invalid_reference') return '信息未查到'
  return '行程提示'
}

function costGapText(unknownCount: number, conflictCount: number): string {
  const parts: string[] = []
  if (unknownCount) parts.push(`${unknownCount} 项未查到`)
  if (conflictCount) parts.push(`${conflictCount} 项存在冲突`)
  return `消费信息：${parts.join('，')}`
}

function costEntryText(entry: ReferenceCostEntry): string {
  if (entry.status === 'unknown' || entry.amount === null) return '金额未查询到'
  if (entry.status === 'conflict') return '金额存在冲突'
  return `¥${entry.amount}${entry.kind === 'lodging' ? ' · 住宿单列' : ' / 人'}`
}

function providerLabel(provider: string): string {
  return provider === 'amap' ? '高德地图' : provider
}

function formatCityFilename(city: string): string {
  return city.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 48) || '旅行计划'
}

function textChunks(value: string, size = 400): string[] {
  const chunks: string[] = []
  let remaining = value
  while (remaining.length > size) {
    let end = remaining.lastIndexOf('\n', size)
    if (end < Math.floor(size * 0.55)) end = remaining.lastIndexOf(' ', size)
    if (end < Math.floor(size * 0.55)) end = size
    chunks.push(remaining.slice(0, end))
    remaining = remaining.slice(end)
  }
  if (remaining) chunks.push(remaining)
  return chunks.length ? chunks : ['']
}

function splitTextTarget(block: HTMLElement): HTMLElement | null {
  const candidates = [...block.querySelectorAll<HTMLElement>('p, li, small, span, b, h2, h3')]
  candidates.sort((left, right) => (right.textContent?.length ?? 0) - (left.textContent?.length ?? 0))
  return candidates[0] ?? (block.matches('p, li, small, span, b, h2, h3') ? block : null)
}

function measureBlockHeight(block: HTMLElement): number {
  const probe = document.createElement('div')
  probe.className = 'v4-export-measure'
  probe.append(block.cloneNode(true))
  document.body.append(probe)
  try {
    return probe.scrollHeight
  } finally {
    probe.remove()
  }
}

function splitOversizedBlock(block: HTMLElement, depth = 0): HTMLElement[] {
  if (depth > 8) throw new Error('有一段行程文字过长，无法安全分页。')
  if (measureBlockHeight(block) <= MAX_BLOCK_HEIGHT) return [block.cloneNode(true) as HTMLElement]
  const target = splitTextTarget(block)
  const value = target?.textContent ?? ''
  if (!target || value.length < 2) throw new Error('有一段行程内容过长，无法安全分页。')
  const chunkSize = Math.min(400, Math.max(1, Math.ceil(value.length / 2)))
  const chunks = textChunks(value, chunkSize)
  if (chunks.length < 2 || chunks.join('') !== value) throw new Error('长文本分页校验失败，未生成不完整图片。')
  const pieces = chunks.map((text, index) => {
    if (index === 0) {
      const clone = block.cloneNode(true) as HTMLElement
      const cloneTarget = splitTextTarget(clone)
      if (!cloneTarget) throw new Error('长文本分页失败，未生成不完整图片。')
      cloneTarget.textContent = text
      return clone
    }
    const continuation = target.cloneNode(true) as HTMLElement
    continuation.textContent = text
    continuation.classList.add('v4-export-continuation')
    return continuation
  })
  const expanded: HTMLElement[] = []
  for (const piece of pieces) {
    expanded.push(...(measureBlockHeight(piece) > MAX_BLOCK_HEIGHT ? splitOversizedBlock(piece, depth + 1) : [piece]))
  }
  return expanded
}

function makeFragment(className: string, content: HTMLElement): HTMLElement {
  const wrapper = document.createElement('div')
  wrapper.className = className
  wrapper.append(content.cloneNode(true))
  return wrapper
}

function issueFragments(container: HTMLElement): HTMLElement[] {
  return [...container.children].map((child) => {
    const clone = child.cloneNode(true) as HTMLElement
    if (clone.tagName === 'P') clone.classList.add('v4-export-issue-row')
    return clone
  })
}

function activityFragments(activity: HTMLElement): HTMLElement[] {
  return [...activity.children].map((child) => makeFragment('v4-export-activity v4-export-activity-fragment', child as HTMLElement))
}

function routeFragments(route: HTMLElement): HTMLElement[] {
  return [...route.children].map((child) => makeFragment('v4-export-route v4-export-route-fragment', child as HTMLElement))
}

function collectDayBlocks(dayElement: HTMLElement): HTMLElement[] {
  const blocks: HTMLElement[] = []
  const globalIssues = dayElement.querySelector<HTMLElement>(':scope > .v4-export-global-issues')
  if (globalIssues) blocks.push(...issueFragments(globalIssues))

  const grid = dayElement.querySelector<HTMLElement>('.v4-export-grid')
  const main = grid?.querySelector<HTMLElement>(':scope > main')
  for (const child of [...(main?.children ?? [])] as HTMLElement[]) {
    if (child.classList.contains('v4-export-issues')) {
      blocks.push(...issueFragments(child))
    } else if (child.classList.contains('v4-export-timeline')) {
      const children = [...child.children] as HTMLElement[]
      const heading = children.find((item) => item.tagName === 'H3')
      if (heading) blocks.push(heading)
      for (const item of children.filter((node) => node !== heading)) {
        blocks.push(...(item.classList.contains('v4-export-activity') ? activityFragments(item) : [item]))
      }
    } else if (child.classList.contains('v4-export-section') && child.querySelector('.v4-export-route')) {
      const heading = [...child.children].find((item) => (item as HTMLElement).tagName === 'H3') as HTMLElement | undefined
      if (heading) blocks.push(heading)
      for (const route of [...child.querySelectorAll<HTMLElement>(':scope > .v4-export-route')]) blocks.push(...routeFragments(route))
    } else if (child.classList.contains('v4-export-return')) {
      for (const item of [...child.children] as HTMLElement[]) {
        blocks.push(...(item.classList.contains('v4-export-route') ? routeFragments(item) : [makeFragment('v4-export-return', item)]))
      }
    } else {
      blocks.push(child)
    }
  }

  const aside = grid?.querySelector<HTMLElement>(':scope > aside')
  for (const card of [...(aside?.children ?? [])] as HTMLElement[]) {
    const heading = [...card.children].find((item) => (item as HTMLElement).tagName === 'B') as HTMLElement | undefined
    if (heading) blocks.push(heading)
    if (card.classList.contains('v4-export-cost-list')) {
      blocks.push(...[...card.children].filter((item) => item !== heading).map((item) => {
        const clone = item.cloneNode(true) as HTMLElement
        clone.classList.add('v4-export-cost-entry')
        return clone
      }))
    } else {
      const list = card.querySelector<HTMLElement>(':scope > ol')
      if (list) {
        for (const item of [...list.children] as HTMLElement[]) {
          const clone = item.cloneNode(true) as HTMLElement
          clone.classList.add('v4-export-destination-item')
          blocks.push(clone)
        }
      } else {
        const details = [...card.children].filter((item) => item !== heading) as HTMLElement[]
        blocks.push(...details)
      }
    }
  }
  return blocks
}

function paginateDay(dayElement: HTMLElement): HTMLElement[] {
  if (dayElement.scrollHeight <= MAX_PAGE_HEIGHT) return [dayElement]
  const header = dayElement.querySelector<HTMLElement>(':scope > .v4-export-header')
  const blocks = collectDayBlocks(dayElement)
  if (!blocks.length) throw new Error('行程内容过长，无法完整分页。')
  const pages: HTMLElement[] = []
  let currentPage = document.createElement('article')
  let currentContent = document.createElement('div')
  let hasContent = false

  const startPage = () => {
    currentPage = document.createElement('article')
    currentPage.className = 'v4-export-sheet v4-export-slice'
    if (header) currentPage.append(header.cloneNode(true))
    currentContent = document.createElement('div')
    currentContent.className = 'v4-export-page-content'
    currentPage.append(currentContent)
    document.body.append(currentPage)
    hasContent = false
  }
  const finishPage = () => pages.push(currentPage)

  try {
    startPage()
    for (const original of blocks) {
      for (const piece of splitOversizedBlock(original)) {
        currentContent.append(piece)
        if (currentPage.scrollHeight > MAX_PAGE_HEIGHT && hasContent) {
          piece.remove()
          finishPage()
          startPage()
          currentContent.append(piece)
        }
        if (currentPage.scrollHeight > MAX_PAGE_HEIGHT) {
          piece.remove()
          throw new Error('有一段行程内容超过图片安全尺寸，未生成不完整图片。')
        }
        hasContent = true
      }
    }
    if (hasContent) finishPage()
    if (!pages.length) {
      startPage()
      finishPage()
    }
    if (pages.some((page) => page.scrollHeight > MAX_PAGE_HEIGHT)) throw new Error('行程图片分页超过浏览器安全尺寸。')
    return pages
  } catch (error) {
    for (const page of new Set([...pages, currentPage])) page.remove()
    throw error
  }
}

async function renderAll(): Promise<readonly V4ExportFile<Blob>[]> {
  await nextTick()
  if (document.fonts?.ready) await document.fonts.ready
  if (props.visible.view.days.length === 0) throw new Error('当前行程没有可导出的日期。')
  const output: V4ExportFile<Blob>[] = []
  const temporaryPages: HTMLElement[] = []
  const city = formatCityFilename(props.visible.view.city)
  try {
    for (const [index, day] of props.visible.view.days.entries()) {
      const element = dayElements.get(day.id)
      if (!element) throw new Error(`第 ${index + 1} 天的图片区域尚未准备好。`)
      const pages = paginateDay(element)
      temporaryPages.push(...pages.filter((page) => page.classList.contains('v4-export-slice')))
      for (const [pageIndex, page] of pages.entries()) {
        const canvas = await html2canvas(page, {
          backgroundColor: '#fffdf8',
          scale: 2,
          useCORS: true,
          logging: false,
          windowWidth: 1080,
        })
        const blob = await new Promise<Blob>((resolve, reject) => {
          canvas.toBlob((result) => result ? resolve(result) : reject(new Error(`第 ${index + 1} 天图片生成失败。`)), 'image/png')
        })
        const pageSuffix = pages.length > 1 ? `_续页${pageIndex + 1}` : ''
        output.push({ filename: `旅行计划_${city}_第${index + 1}天_${day.date}${pageSuffix}.png`, blob })
      }
    }
  } finally {
    for (const page of temporaryPages) page.remove()
  }
  return output
}

defineExpose({ renderAll })
</script>

<style>
.v4-export-host { position: fixed; top: 0; left: -12000px; z-index: -1; width: 1080px; pointer-events: none; }
.v4-export-sheet { box-sizing: border-box; width: 1080px; min-height: 800px; padding: 48px; color: #203a35; background: #fffdf8; font-family: 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif; }
.v4-export-header { display: flex; justify-content: space-between; gap: 30px; padding-bottom: 24px; border-bottom: 2px solid #dce9e1; }
.v4-export-header h1 { margin: 8px 0; font: 600 34px 'Songti SC', 'Noto Serif SC', serif; }.v4-export-header p { color: #667c75; }
.v4-export-header small, .v4-export-muted, .v4-export-sheet small { color: #667c75; font-size: 12px; line-height: 1.7; }
.v4-export-cost { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; max-width: 360px; text-align: right; }.v4-export-cost strong { color: #244032; font: 30px 'Songti SC', 'Noto Serif SC', serif; }
.v4-export-global-issues, .v4-export-issues { margin: 18px 0; padding: 14px 18px; border-radius: 10px; color: #81553d; background: #fbf3e8; line-height: 1.7; }.v4-export-global-issues p,.v4-export-issues p { margin: 4px 0; }
.v4-export-grid { display: grid; grid-template-columns: 1.35fr .85fr; gap: 24px; margin-top: 22px; align-items: start; }.v4-export-grid main,.v4-export-grid aside { min-width: 0; }
.v4-export-section { margin: 0 0 18px; }.v4-export-section-title { display: flex; align-items: baseline; gap: 12px; }.v4-export-section-title small { color: #a65c40; letter-spacing: .12em; font-weight: 700; }.v4-export-section-title h2 { margin: 0; color: #254130; font: 25px 'Songti SC', 'Noto Serif SC', serif; }.v4-export-section h3 { margin: 0 0 10px; color: #355342; font: 18px 'Songti SC', 'Noto Serif SC', serif; }
.v4-export-facts { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 12px; }.v4-export-facts span { padding: 7px 9px; color: #45624f; background: #edf2e4; font-size: 11px; }
.v4-export-card,.v4-export-route { margin: 0 0 13px; padding: 15px 17px; border: 1px solid #d9dfd3; border-radius: 9px; background: #f7f8f1; line-height: 1.7; overflow-wrap: anywhere; }.v4-export-card p,.v4-export-route p { margin: 6px 0; color: #61746a; font-size: 12px; }.v4-export-card > b { display: block; margin-bottom: 6px; color: #355342; }
.v4-export-lodging { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }.v4-export-lodging > div + div { border-left: 1px solid #dce3d3; padding-left: 16px; }.v4-export-lodging b { display: block; margin: 4px 0; }.v4-export-lodging small { display: block; }
.v4-export-timeline { margin-top: 20px; }.v4-export-activity { margin: 10px 0; padding: 14px 16px; border: 1px solid #dce7e0; border-radius: 10px; background: #fffdfa; overflow-wrap: anywhere; }.v4-export-activity p { margin: 6px 0; color: #61746a; font-size: 12px; line-height: 1.7; }.v4-export-activity-head { display: flex; justify-content: space-between; gap: 14px; }.v4-export-activity-head b { color: #355342; }.v4-export-activity-head span { flex: 0 0 auto; color: #63745f; font-size: 11px; }.v4-export-activity > small { display: block; margin-top: 4px; }.v4-export-cost-detail { margin-top: 7px; color: #59704f; font-size: 12px; }
.v4-export-missing,.v4-export-issue-row,.v4-export-return { margin: 9px 0; padding: 12px 14px; border: 1px dashed #c8b791; border-radius: 8px; color: #64563c; background: #fbf7eb; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }.v4-export-missing p { margin: 5px 0; }.v4-export-missing small { display: block; }.v4-export-issue-row { display: flex; gap: 10px; padding: 8px 10px; border-style: solid; }.v4-export-issue-row b { flex: 0 0 auto; }.v4-export-return { margin-top: 18px; border-style: solid; }.v4-export-return p { margin: 6px 0; }
.v4-export-route { background: #f3f7f4; }.v4-export-route > small { display: block; margin-top: 5px; }.v4-export-modes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-top: 8px; }.v4-export-modes span { padding: 7px; border: 1px solid #e0e7d5; color: #657765; font-size: 10px; }.v4-export-modes span.chosen { border-color: #789272; background: #eaf1df; color: #355342; }
.v4-export-card ol { margin: 10px 0 0; padding-left: 22px; }.v4-export-card li { padding: 7px 0; border-bottom: 1px solid #e7ece2; }.v4-export-card li small { display: block; }.v4-export-cost-list > div { padding: 9px 0; border-top: 1px solid #e7ece2; }.v4-export-cost-list > div span,.v4-export-cost-list > div b,.v4-export-cost-list > div small { display: block; }.v4-export-cost-list > div b { margin: 3px 0; color: #4c684f; font-size: 12px; }
.v4-export-page-content { display: flex; flex-direction: column; gap: 8px; margin-top: 22px; }.v4-export-page-content > h3 { margin: 6px 0 0; color: #355342; font: 18px 'Songti SC', 'Noto Serif SC', serif; }.v4-export-destination-item { padding: 8px 0; border-bottom: 1px solid #e7ece2; line-height: 1.7; }.v4-export-cost-entry { padding: 9px 0; border-top: 1px solid #e7ece2; line-height: 1.7; overflow-wrap: anywhere; }.v4-export-cost-entry span,.v4-export-cost-entry b,.v4-export-cost-entry small { display: block; }.v4-export-cost-entry b { margin: 3px 0; color: #4c684f; font-size: 12px; }.v4-export-continuation { margin-top: 4px; padding: 8px 12px; border-left: 3px solid #b2c7a6; color: #61746a; background: #f7f8f1; line-height: 1.7; overflow-wrap: anywhere; }.v4-export-activity-fragment,.v4-export-route-fragment { margin-bottom: 0; }
.v4-export-slice { position: fixed; top: 0; left: -12000px; z-index: -1; }
.v4-export-measure { position: fixed; top: 0; left: -12000px; z-index: -2; width: 984px; display: flex; flex-direction: column; }
</style>
