import type {
  DayReferenceCostSummary,
  DraftActivity,
  DraftDay,
  ItineraryPreview,
  PlaceFact,
  ReferenceCostEntry,
  RouteFact,
  RouteLeg,
  RouteOption,
  V4PreviewEnvelope,
  V4SlotPeriod,
  V4TravelMode,
  ValidationIssue,
  ValidationDay,
} from '@/types/v4Preview'

export type GeoPoint = [longitude: number, latitude: number]

export interface PreviewCostView {
  status: ReferenceCostEntry['status'] | 'unavailable'
  amount: string | null
  text: string
  sourceLabel: string | null
  queriedAt: string | null
  evidenceLabel: string | null
}

export interface PreviewActivityView {
  id: string
  slotId: string
  candidateRef: string | null
  placeFactId: string | null
  poiId: string | null
  kind: DraftActivity['kind']
  period: V4SlotPeriod | null
  periodLabel: string
  title: string
  address: string
  area: string
  description: string
  cost: PreviewCostView
  issues: ValidationIssue[]
  coordinate: GeoPoint | null
}

export interface PreviewPath {
  mode: 'transit' | 'bicycling' | 'walking' | 'bus' | 'subway' | 'railway'
  coordinates: GeoPoint[]
}

export interface PreviewModeView {
  mode: V4TravelMode
  label: string
  status: string
  optionId: string | null
  durationSeconds: number | null
  durationLabel: string
}

export interface PreviewLegView {
  id: string
  isReturn: boolean
  fromLabel: string
  toLabel: string
  selectedMode: V4TravelMode | null
  selectedModeLabel: string
  selectedDurationLabel: string
  modes: PreviewModeView[]
  geometryStatus: 'complete' | 'partial' | 'missing' | 'not_selected' | 'not_required'
  paths: PreviewPath[]
  geometryMessage: string
  timeBasisLabel: string | null
  issues: ValidationIssue[]
}

export interface PreviewMissingSlot {
  id: string
  kind: 'missing' | 'conditional'
  period: V4SlotPeriod
  periodLabel: string
  prompt: string
  reason: string | null
  issues: ValidationIssue[]
}

export type PreviewTimelineItem =
  | { type: 'activity'; activity: PreviewActivityView; position: number; incomingLeg: PreviewLegView | null }
  | { type: 'missing_slot'; slot: PreviewMissingSlot }
  | { type: 'slot_issue'; issue: ValidationIssue; periodLabel: string }

export interface PreviewLocationView {
  id: string
  name: string
  address: string
  area: string
  kind: 'activity' | 'hotel' | 'area'
  coordinate: GeoPoint | null
  number?: number
}

export interface PreviewDayView {
  id: string
  date: string
  theme: string
  activities: PreviewActivityView[]
  timeline: PreviewTimelineItem[]
  legs: PreviewLegView[]
  missingSlots: PreviewMissingSlot[]
  locations: PreviewLocationView[]
  hotel: {
    label: string
    name: string
    address: string
    coordinate: GeoPoint | null
    cost: PreviewCostView
  }
  area: {
    label: string
    coordinate: GeoPoint | null
  } | null
  weather: {
    known: boolean
    summary: string
    advice: string
  }
  transport: {
    knownTravelSeconds: number
    unknownLegCount: number
  }
  returnLeg: PreviewLegView | null
  cost: {
    status: DayReferenceCostSummary['status'] | 'unavailable'
    knownSubtotal: string | null
    unknownCount: number
    conflictCount: number
    entries: ReferenceCostEntry[]
  }
  issues: ValidationIssue[]
  unboundIssues: ValidationIssue[]
  recommendations: PreviewRecommendation[]
}

export interface PreviewRecommendation {
  id: string
  kind: 'sightseeing' | 'meal'
  name: string
  address: string
  area: string
  reason: string
  cost: PreviewCostView
}

export interface TripPreviewView {
  city: string
  arrivalAt: string
  departureAt: string
  timezone: string
  tripBudget: string | null
  planIssues: ValidationIssue[]
  cost: {
    status: V4PreviewEnvelope['cost']['status']
    knownSubtotal: string | null
    unknownCount: number
    conflictCount: number
  }
  days: PreviewDayView[]
}

const PERIOD_ORDER: V4SlotPeriod[] = ['breakfast', 'morning', 'lunch', 'afternoon', 'dinner', 'evening']
const PERIOD_LABEL: Record<V4SlotPeriod, string> = {
  breakfast: '早餐', morning: '上午', lunch: '午餐', afternoon: '下午', dinner: '晚餐', evening: '晚上',
}
const MODE_LABEL: Record<V4TravelMode, string> = {
  transit: '公共交通', bicycling: '骑行', walking: '步行',
}

// S01只收敛用户展示；后端暂时仍会返回旧时间报告，不能让它从提示区泄漏。
const HIDDEN_SCHEDULE_ISSUES = new Set([
  'must_visit_unplanned', // 兼容移除必去绑定检查前保存的快照。
  'cost_unavailable', // 消费缺失统一在各地点的消费位置提示。
  'timing_not_provided', 'activity_timing_unverified', 'activity_exceeds_available_end',
  'departure_day_below_minimum_with_activities', 'departure_day_not_required',
  'return_deadline_conflict', 'return_timing_unverified', 'return_route_unverified',
  'opening_hours_conflict', 'opening_hours_unavailable', 'opening_hours_unverified',
])

export function formatReferenceDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '时间未核实'
  const minutes = Math.round(seconds / 60)
  return `${Math.floor(minutes / 60)}小时${minutes % 60}分钟`
}

export function formatReferenceAmount(amount: string | number | null | undefined): string | null {
  if (amount == null || amount === '') return null
  const value = Number(amount)
  if (!Number.isFinite(value) || value < 0) return null
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
}

/** 用户页面和导出共用的本地中文查询时间格式。 */
export function formatPreviewEvidenceTime(value: string | null | undefined): string | null {
  if (!value) return null
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return null
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

// 从已有消费明细计算展示小计，未知和冲突金额不计入。
function knownCostSubtotal(entries: ReferenceCostEntry[]): string | null {
  const amounts = entries.filter((entry) => entry.status === 'known' && entry.amount !== null)
    .map((entry) => Number(entry.amount)).filter((amount) => Number.isFinite(amount) && amount >= 0)
  if (!amounts.length) return null
  return formatReferenceAmount(amounts.reduce((sum, amount) => sum + Math.round(amount * 100), 0) / 100)
}

function knownCostStatus(entries: ReferenceCostEntry[]): TripPreviewView['cost']['status'] {
  if (!entries.length) return 'no_activities'
  if (knownCostSubtotal(entries) === null) return 'all_unknown'
  return entries.every((entry) => entry.status === 'known') ? 'all_known' : 'known_partial'
}

export function mapV4Preview(envelope: V4PreviewEnvelope): TripPreviewView {
  const preview = envelope.preview
  const displayIssues = preview.validation.issues.filter((issue) => {
    if (HIDDEN_SCHEDULE_ISSUES.has(issue.code)) return false
    // 跨日景点去重的笼统核实提示不进入预览和导出；保留实际冲突及其他需求提示。
    if (issue.code === 'requirement_unverified'
      && /^无法仅根据当前结构化信息核实要求“(?:无充分理由|无理由)不要跨(?:天|日)重复(?:安排)?(?:同一|相同)?景点[。.]?”是否落实[。.]?$/.test(issue.message)) return false
    // 兼容旧快照：选填地点为空时不展示历史核实提示。
    if (issue.code === 'ENDPOINT_ARRIVAL_UNKNOWN') return Boolean(preview.planning_input.request.arrival_place_query?.trim())
    if (issue.code === 'ENDPOINT_DEPARTURE_UNKNOWN') return Boolean(preview.planning_input.request.departure_place_query?.trim())
    return true
  })
  const hasSpecifiedLodging = Boolean(
    preview.planning_input.request.lodging_poi_id?.trim()
    || preview.planning_input.request.lodging_query?.trim(),
  )
  const placesByFact = new Map(preview.facts.places.map((place) => [place.fact_id, place]))
  const placesByPoi = new Map(preview.facts.places.map((place) => [place.poi_id, place]))
  const candidatesByRef = new Map(preview.candidates.map((candidate) => [candidate.candidate_id, candidate]))
  const transportByDay = new Map(preview.day_summaries.map((day) => [day.day_id, day]))
  const costByDay = new Map(envelope.cost.days.map((day) => [day.day_id, day]))
  const dayConstraints = new Map(preview.planning_input.days.map((day) => [day.day_id, day]))
  const validationsByDay = new Map(preview.validation.days.map((day) => [day.day_id, day]))
  const routesByFact = new Map(preview.facts.routes.map((route) => [route.fact_id, route]))
  const sourcesById = new Map((preview.facts.sources ?? []).map((source) => [source.tool_call_id, source]))
  const departureDate = preview.planning_input.request.departure_at.slice(0, 10)
  const entriesForDay = (day: typeof envelope.cost.days[number]) => [
    ...day.entries,
    ...(day.lodging && day.day_date < departureDate ? [day.lodging] : []),
  ]
  const tripCostEntries = envelope.cost.days.flatMap(entriesForDay)
  const tripUnknown = tripCostEntries.filter((entry) => entry.status === 'unknown').length
  const tripConflicts = tripCostEntries.filter((entry) => entry.status === 'conflict').length
  const subtotal = knownCostSubtotal(tripCostEntries)

  const days: PreviewDayView[] = preview.draft.days.map((day) => {
    const constraint = dayConstraints.get(day.day_id)
    const dayTransport = transportByDay.get(day.day_id)
    const dayCost = costByDay.get(day.day_id)
    const costEntries = dayCost ? entriesForDay(dayCost) : []
    const activityCost = new Map<string, ReferenceCostEntry>()
    for (const entry of dayCost?.entries ?? []) {
      for (const activityId of entry.activity_ids) activityCost.set(activityId, entry)
    }
    const issues = displayIssues
      .filter((issue) => issue.day_id === day.day_id)
      .map(mapValidationIssue)
    const activityViews = day.activities.map((activity) => mapActivity(
      activity,
      placesByFact,
      activityCost.get(activity.activity_id),
      constraint?.slots.find((slot) => slot.slot_id === activity.slot_id)?.period ?? null,
      issues,
    ))
    const legs = preview.draft.legs
      .filter((leg) => leg.day_id === day.day_id)
      .map((leg) => mapLeg(
        leg, day.activities, placesByFact, placesByPoi, routesByFact, issues,
        preview.planning_input.request.timezone,
      ))
    const incomingLegByActivity = new Map(
      preview.draft.legs
        .filter((leg) => leg.day_id === day.day_id && leg.to_endpoint.kind === 'activity')
        .map((leg) => [leg.to_endpoint.reference_id, legs.find((view) => view.id === leg.leg_id) ?? null]),
    )
    const missingSlots = buildMissingSlots(
      constraint?.slots ?? [],
      validationsByDay.get(day.day_id),
      issues,
    )
    const hiddenSlotIssues = collectUnboundSlotIssues(issues, constraint?.slots ?? [], activityViews, missingSlots)
    const timeline = buildTimeline(activityViews, missingSlots, constraint?.slots ?? [], incomingLegByActivity, hiddenSlotIssues)
    const visibleIssueIds = new Set([
      ...issues.filter((issue) => issue.scope === 'day').map((issue) => issue.issue_id),
      ...activityViews.flatMap((activity) => activity.issues.map((issue) => issue.issue_id)),
      ...legs.flatMap((leg) => leg.issues.map((issue) => issue.issue_id)),
      ...missingSlots.flatMap((slot) => slot.issues.map((issue) => issue.issue_id)),
      ...hiddenSlotIssues.map((item) => item.issue.issue_id),
    ])
    const unboundIssues = issues.filter((issue) => !visibleIssueIds.has(issue.issue_id))
    const lodgingFact = day.lodging?.place_fact_id
      ? placesByFact.get(day.lodging.place_fact_id)
      : day.lodging?.poi_id ? placesByPoi.get(day.lodging.poi_id) : undefined
    const lodgingCandidate = day.lodging?.candidate_ref
      ? candidatesByRef.get(day.lodging.candidate_ref)
      : undefined
    const areaHintId = day.lodging?.area_hint_id ?? lodgingCandidate?.area_hint_id
    const areaHint = preview.draft.area_hints.find((hint) => hint.area_hint_id === areaHintId)
    const lodgingEntry = dayCost?.lodging ?? null
    const hotelCost = mapCost(lodgingEntry, '未查询到可供参考的人均消费')
    const lodgingChange = preview.planning_input.requirements?.lodging_changes
      ?.filter((change) => change.start_date <= day.day_date)
      .sort((left, right) => right.start_date.localeCompare(left.start_date))[0]
    const dayHasSpecifiedLodging = lodgingChange ? Boolean(lodgingChange.lodging_query?.trim()) : hasSpecifiedLodging
    const hotelLabel = dayHasSpecifiedLodging || day.lodging?.selection_source === 'user'
      ? '已指定住处'
      : day.lodging?.selection_source === 'recommended' ? '推荐酒店' : '住宿参考地点'
    const hotelName = lodgingFact?.name ?? '具体住宿地点未核实'
    const hotelAddress = lodgingFact?.address ?? ''
    const area = dayHasSpecifiedLodging || day.lodging?.selection_source === 'user' ? null : {
      label: areaHint?.label ?? '住宿区域未确定',
      coordinate: toGeoPoint(areaHint?.center),
    }
    const weather = mapWeather(day, preview)
    const locations: PreviewLocationView[] = activityViews
      .flatMap((activity, activityIndex) => activity.kind === 'free_time' ? [] : [{
        id: activity.id,
        name: activity.title,
        address: activity.address,
        area: activity.area,
        kind: 'activity' as const,
        coordinate: activity.coordinate,
        number: activityIndex + 1,
      }])
    if (lodgingFact) locations.unshift({
      id: `hotel:${day.day_id}`,
      name: lodgingFact.name,
      address: lodgingFact.address ?? '',
      area: lodgingFact.business_area ?? '',
      kind: 'hotel',
      coordinate: toGeoPoint(lodgingFact.location),
    })
    if (areaHint && area) locations.push({
      id: `area:${areaHint.area_hint_id}`,
      name: areaHint.label,
      address: '推荐住宿区域参考点',
      area: '',
      kind: 'area',
      coordinate: area.coordinate,
    })
    const daySubtotal = knownCostSubtotal(costEntries)

    return {
      id: day.day_id,
      date: day.day_date,
      theme: day.theme || '今日行程',
      activities: activityViews,
      timeline,
      legs,
      missingSlots,
      locations,
      hotel: {
        label: hotelLabel,
        name: hotelName,
        address: hotelAddress,
        coordinate: toGeoPoint(lodgingFact?.location),
        cost: hotelCost,
      },
      area,
      weather,
      transport: {
        knownTravelSeconds: dayTransport?.known_travel_s ?? 0,
        unknownLegCount: dayTransport?.unknown_leg_ids.length ?? 0,
      },
      cost: {
        status: knownCostStatus(costEntries),
        knownSubtotal: daySubtotal,
        unknownCount: costEntries.filter((entry) => entry.status === 'unknown').length,
        conflictCount: costEntries.filter((entry) => entry.status === 'conflict').length,
        entries: costEntries,
      },
      returnLeg: legs.find((leg) => leg.isReturn)
        ?? null,
      issues,
      unboundIssues,
      recommendations: mapRecommendations(day, preview, placesByFact, sourcesById, costEntries),
    }
  })

  return {
    city: preview.planning_input.request.city,
    arrivalAt: preview.planning_input.request.arrival_at,
    departureAt: preview.planning_input.request.departure_at,
    timezone: preview.planning_input.request.timezone,
    tripBudget: formatReferenceAmount(preview.planning_input.request.budget?.amount ?? null),
    planIssues: displayIssues
      .filter((issue) => issue.day_id === null || issue.scope === 'plan')
      .map(mapValidationIssue),
    cost: {
      status: knownCostStatus(tripCostEntries),
      knownSubtotal: subtotal,
      unknownCount: tripUnknown,
      conflictCount: tripConflicts,
    },
    days,
  }
}

function mapActivity(
  activity: DraftActivity,
  placesByFact: Map<string, PlaceFact>,
  costEntry: ReferenceCostEntry | undefined,
  period: V4SlotPeriod | null,
  dayIssues: ValidationIssue[],
): PreviewActivityView {
  const place = activity.place_fact_id ? placesByFact.get(activity.place_fact_id) : undefined
  const cost = mapCost(costEntry ?? null, '未查询到可供参考的人均消费')
  const periodLabel = period ? PERIOD_LABEL[period] : '行程安排'
  return {
    id: activity.activity_id,
    slotId: activity.slot_id,
    candidateRef: activity.candidate_ref,
    placeFactId: activity.place_fact_id,
    poiId: activity.poi_id,
    kind: activity.kind,
    period,
    periodLabel,
    title: activity.kind === 'free_time' ? '自由活动' : place?.name ?? '地点信息未核实',
    address: place?.address ?? '',
    area: place?.business_area ?? '',
    description: activity.recommendation_reason,
    cost,
    issues: dayIssues.filter((issue) => (
      issue.scope === 'activity' && issue.target_id === activity.activity_id
    ) || (
      issue.scope === 'slot'
      && issue.target_id === activity.slot_id
      && issue.category !== 'missing_arrangement'
      && issue.code !== 'conditional_slot_unresolved'
    )),
    coordinate: toGeoPoint(place?.location),
  }
}

function mapCost(entry: ReferenceCostEntry | null, unknownText: string): PreviewCostView {
  if (!entry) return { status: 'unavailable', amount: null, text: unknownText, sourceLabel: null, queriedAt: null, evidenceLabel: null }
  const source = entry.evidence
    .map((evidence) => evidence.cost_source ?? evidence.sources?.[0])
    .find((candidate) => candidate != null)
  const sourceLabel = source?.provider === 'amap' ? '高德地图' : source?.provider ?? null
  const queriedAt = source?.queried_at ?? null
  const evidenceLabel = formatCostEvidence(sourceLabel, queriedAt)
  if (entry.status === 'known') {
    const amount = formatReferenceAmount(entry.amount)
    return amount == null
      ? { status: 'unavailable', amount: null, text: '消费参考暂不可用', sourceLabel, queriedAt, evidenceLabel }
      : { status: 'known', amount, text: `¥${amount} / 人${entry.kind === 'lodging' ? ' / 晚' : ''}`, sourceLabel, queriedAt, evidenceLabel }
  }
  if (entry.status === 'conflict') return { status: 'conflict', amount: null, text: '消费参考存在冲突', sourceLabel, queriedAt, evidenceLabel }
  return { status: 'unknown', amount: null, text: unknownText, sourceLabel, queriedAt, evidenceLabel }
}

function mapLeg(
  leg: RouteLeg,
  activities: DraftActivity[],
  placesByFact: Map<string, PlaceFact>,
  placesByPoi: Map<string, PlaceFact>,
  routesByFact: Map<string, RouteFact>,
  dayIssues: ValidationIssue[],
  timezone: string,
): PreviewLegView {
  const fromLabel = endpointLabel(leg, 'from', activities, placesByFact, placesByPoi)
  const toLabel = endpointLabel(leg, 'to', activities, placesByFact, placesByPoi)
  const samePlace = leg.selection_source === 'same_place'
  const modes: PreviewModeView[] = samePlace ? [] : (['transit', 'bicycling', 'walking'] as const).map((mode) => {
    const result = leg.mode_results.find((candidate) => candidate.mode === mode)
    const routeFact = result?.route_fact_id ? routesByFact.get(result.route_fact_id) : undefined
    const option = chooseRouteOption(routeFact, result?.option_ids ?? [], mode === leg.selected_mode ? leg.selected_option_id : null)
    const seconds = result?.status === 'available' ? option?.duration_s ?? null : null
    return {
      mode,
      label: MODE_LABEL[mode],
      status: routeModeStatus(result?.status),
      optionId: result?.status === 'available' ? option?.option_id ?? null : null,
      durationSeconds: seconds,
      durationLabel: result?.status === 'available'
        ? formatReferenceDuration(seconds)
        : routeModeStatus(result?.status),
    }
  })
  const selectedMode = leg.selected_mode
  const selectedResult = selectedMode ? leg.mode_results.find((result) => result.mode === selectedMode) : undefined
  const selectedFact = selectedResult?.route_fact_id ? routesByFact.get(selectedResult.route_fact_id) : undefined
  const selectedOption = selectedFact?.options.find((option) => option.option_id === leg.selected_option_id)
  const geometryStatus = samePlace ? 'not_required' : selectedMode
    ? selectedOption?.geometry_status ?? 'missing'
    : 'not_selected'
  const paths = !samePlace && selectedOption ? routeOptionPaths(selectedOption) : []
  const selectedDuration = samePlace ? 0 : selectedOption?.duration_s ?? null
  const geometryMessage = samePlace
    ? '起终点为同一地点，无需交通，也不绘制路径。'
    : selectedMode == null
    ? '尚无已采用路线，地图不绘制连接线。'
    : geometryStatus === 'missing' || paths.length === 0
      ? '路线坐标未返回，地图不绘制连接线。'
      : geometryStatus === 'partial'
        ? '部分路段坐标未返回；地图只显示有真实坐标的路段。'
        : '地图路径与所采用方式及耗时来自同一查询方案。'
  return {
    id: leg.leg_id,
    isReturn: leg.to_endpoint.kind === 'departure',
    fromLabel,
    toLabel,
    selectedMode,
    selectedModeLabel: samePlace ? '同一地点，无需交通' : selectedMode ? MODE_LABEL[selectedMode] : '未采用路线',
    selectedDurationLabel: samePlace ? formatReferenceDuration(0) : selectedMode ? formatReferenceDuration(selectedDuration) : '耗时未核实',
    modes,
    geometryStatus,
    paths,
    geometryMessage,
    timeBasisLabel: routeTimeBasisLabel(leg, timezone),
    issues: dayIssues.filter((issue) => issue.scope === 'leg' && issue.target_id === leg.leg_id),
  }
}

function routeTimeBasisLabel(leg: RouteLeg, timezone: string): string | null {
  if (leg.selection_source === 'same_place') return null
  const requestedAt = leg.requested_departure_at
    ? formatRouteTime(leg.requested_departure_at, timezone)
    : null
  if (leg.time_basis === 'reference') {
    return `公共交通按${requestedAt ? `${requestedAt} ` : ''}参考时刻查询；实际出发时刻未核实。`
  }
  if (leg.time_basis === 'computed') {
    return requestedAt ? `公共交通查询时刻：${requestedAt}；仅作交通参考。` : '公共交通查询仅作交通参考。'
  }
  return requestedAt ? `查询时刻依据未核实（记录时刻：${requestedAt}）。` : '路线查询时刻依据未核实。'
}

function formatRouteTime(value: string, timezone: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: timezone,
  }).format(date)
}

function routeOptionPaths(option: RouteOption): PreviewPath[] {
  if (option.geometry_status === 'missing') return []
  const paths: PreviewPath[] = []
  for (const segment of [...option.segments].sort((a, b) => a.sequence - b.sequence)) {
    const direct = coordinates(segment.polyline)
    if (direct.length >= 2) {
      paths.push({ mode: segment.mode, coordinates: direct })
      continue
    }
    const alternative = segment.transit_alternatives.find((line) => line.mode === segment.mode)
    const alternativePath = coordinates(alternative?.polyline ?? [])
    if (alternativePath.length >= 2) paths.push({ mode: alternative?.mode ?? segment.mode, coordinates: alternativePath })
  }
  return paths
}

function buildMissingSlots(
  slots: Array<{ slot_id: string; period: V4SlotPeriod; eligibility: string; condition_reason?: string | null }>,
  validationDay: ValidationDay | undefined,
  issues: ValidationIssue[],
): PreviewMissingSlot[] {
  if (validationDay?.completeness === 'not_required') return []
  return slots
    .filter((slot) => {
      if (slot.eligibility === 'skipped') return false
      return issues.some((issue) => (
        issue.scope === 'slot'
        && issue.target_id === slot.slot_id
        && (issue.category === 'missing_arrangement' || issue.code === 'conditional_slot_unresolved')
      ))
    })
    .sort((a, b) => PERIOD_ORDER.indexOf(a.period) - PERIOD_ORDER.indexOf(b.period))
    .map((slot) => {
      const relatedIssues = issues.filter((issue) => issue.scope === 'slot' && issue.target_id === slot.slot_id)
      const label = PERIOD_LABEL[slot.period]
      const conditional = relatedIssues.some((issue) => issue.code === 'conditional_slot_unresolved')
      return {
        id: slot.slot_id,
        kind: conditional ? 'conditional' as const : 'missing' as const,
        period: slot.period,
        periodLabel: label,
        prompt: conditional
          ? `${label}安排仍待确认。`
          : isMealPeriod(slot.period) ? `${label}尚未安排餐馆。` : `${label}尚未安排景点。`,
        reason: relatedIssues[0]?.message ?? slot.condition_reason ?? null,
        issues: relatedIssues,
      }
    })
}

function buildTimeline(
  activities: PreviewActivityView[],
  missingSlots: PreviewMissingSlot[],
  slots: Array<{ slot_id: string; period: V4SlotPeriod }>,
  incomingLegs: Map<string, PreviewLegView | null>,
  slotIssues: Array<{ slotId: string; period: V4SlotPeriod; issue: ValidationIssue }>,
): PreviewTimelineItem[] {
  const slotOrder = new Map(slots.map((slot, index) => [slot.slot_id, index]))
  const notices = [
    ...missingSlots.map((slot) => ({ slotId: slot.id, period: slot.period, item: { type: 'missing_slot' as const, slot } })),
    ...slotIssues.map((item) => ({ slotId: item.slotId, period: item.period, item: { type: 'slot_issue' as const, issue: item.issue, periodLabel: PERIOD_LABEL[item.period] } })),
  ].sort((a, b) => (
    (slotOrder.get(a.slotId) ?? PERIOD_ORDER.indexOf(a.period))
    - (slotOrder.get(b.slotId) ?? PERIOD_ORDER.indexOf(b.period))
  ))
  const emitted = new Set<string>()
  const timeline: PreviewTimelineItem[] = []
  const emitMissingBefore = (activitySlotId: string) => {
    const activityOrder = slotOrder.get(activitySlotId) ?? Number.POSITIVE_INFINITY
    for (const notice of notices) {
      const noticeId = notice.item.type === 'missing_slot' ? notice.item.slot.id : notice.item.issue.issue_id
      if (emitted.has(noticeId)) continue
      const noticeOrder = slotOrder.get(notice.slotId) ?? PERIOD_ORDER.indexOf(notice.period)
      if (noticeOrder <= activityOrder) {
        timeline.push(notice.item)
        emitted.add(noticeId)
      }
    }
  }
  activities.forEach((activity, index) => {
    emitMissingBefore(activity.slotId)
    timeline.push({
      type: 'activity',
      activity,
      position: index + 1,
      incomingLeg: incomingLegs.get(activity.id) ?? null,
    })
  })
  for (const notice of notices) {
    const noticeId = notice.item.type === 'missing_slot' ? notice.item.slot.id : notice.item.issue.issue_id
    if (!emitted.has(noticeId)) timeline.push(notice.item)
  }
  return timeline
}

function collectUnboundSlotIssues(
  issues: ValidationIssue[],
  slots: Array<{ slot_id: string; period: V4SlotPeriod; eligibility: string }>,
  activities: PreviewActivityView[],
  missingSlots: PreviewMissingSlot[],
): Array<{ slotId: string; period: V4SlotPeriod; issue: ValidationIssue }> {
  const alreadyVisible = new Set([
    ...activities.flatMap((activity) => activity.issues.map((issue) => issue.issue_id)),
    ...missingSlots.flatMap((slot) => slot.issues.map((issue) => issue.issue_id)),
  ])
  return issues.flatMap((issue) => {
    if (issue.scope !== 'slot' || alreadyVisible.has(issue.issue_id)) return []
    const slot = slots.find((candidate) => candidate.slot_id === issue.target_id)
    if (!slot || slot.eligibility === 'skipped') return []
    return [{ slotId: slot.slot_id, period: slot.period, issue }]
  })
}

function mapRecommendations(
  day: DraftDay,
  preview: ItineraryPreview,
  placesByFact: Map<string, PlaceFact>,
  sourcesById: Map<string, { provider: string; queried_at: string }>,
  entries: ReferenceCostEntry[],
): PreviewRecommendation[] {
  const constraint = preview.planning_input.days.find((item) => item.day_id === day.day_id)
  const daySlots = new Set((constraint?.slots ?? []).map((slot) => slot.slot_id))
  const selected = new Set(day.activities.flatMap((activity) => [
    activity.candidate_ref,
    activity.place_fact_id,
    activity.poi_id,
  ]).filter((value): value is string => Boolean(value)))
  const selectedPois = new Set(day.activities.map((activity) => activity.poi_id).filter((value): value is string => Boolean(value)))
  const costByPoi = new Map(entries.map((entry) => [entry.poi_id, entry]))
  const used = new Set<string>()
  const candidates = preview.candidates.filter((candidate) => {
    if (candidate.category === 'lodging') return false
    if (candidate.slot_ids.length && !candidate.slot_ids.some((slotId) => daySlots.has(slotId))) return false
    if (selected.has(candidate.candidate_id) || selected.has(candidate.place_fact_id) || selected.has(candidate.poi_id)) return false
    if (selectedPois.has(candidate.poi_id) || used.has(`${candidate.category}:${candidate.poi_id}`)) return false
    used.add(`${candidate.category}:${candidate.poi_id}`)
    return true
  })
  const selectedCandidates = [
    ...candidates.filter((candidate) => candidate.category === 'sightseeing').slice(0, 2),
    ...candidates.filter((candidate) => candidate.category === 'meal').slice(0, 1),
  ]
  return selectedCandidates.flatMap((candidate) => {
    const place = placesByFact.get(candidate.place_fact_id)
    if (!place) return []
    const costEntry = costByPoi.get(candidate.poi_id)
    return [{
      id: candidate.candidate_id,
      kind: candidate.category as 'sightseeing' | 'meal',
      name: place.name,
      address: place.address ?? '',
      area: place.business_area ?? '',
      reason: candidate.recommendation_reason,
      cost: costEntry ? mapCost(costEntry, '未查询到可供参考的人均消费') : mapFactCost(place, sourcesById),
    }]
  })
}

function mapWeather(day: DraftDay, preview: ItineraryPreview): PreviewDayView['weather'] {
  const weatherFact = preview.facts.weather.find((fact) => fact.fact_id === day.weather_fact_id)
  const forecast = weatherFact?.forecasts.find((item) => item.forecast_date === day.day_date)
  if (!forecast) {
    return {
      known: false,
      summary: '该日期天气未知',
      advice: '未查到覆盖当天的有效天气预报，因此没有天气依据的出行建议。',
    }
  }
  const dayWeather = forecast.day_weather || '未知'
  const nightWeather = forecast.night_weather || '未知'
  const temperatures = forecast.day_temp_c == null && forecast.night_temp_c == null
    ? ''
    : ` · ${forecast.day_temp_c == null ? '白天温度未知' : `白天${forecast.day_temp_c}°C`}`
      + `${forecast.night_temp_c == null ? '' : ` / 夜间${forecast.night_temp_c}°C`}`
  const advice = day.weather_advice?.trim() || '请按当天预报安排出行，并留意天气变化。'
  return {
    known: true,
    summary: `白天${dayWeather} · 夜间${nightWeather}${temperatures}`,
    advice,
  }
}

function chooseRouteOption(routeFact: RouteFact | undefined, optionIds: string[], preferredOptionId: string | null): RouteOption | undefined {
  if (!routeFact || routeFact.status !== 'available') return undefined
  const allowed = new Set(optionIds)
  const options = routeFact.options.filter((option) => allowed.has(option.option_id))
  const preferred = options.find((option) => option.option_id === preferredOptionId)
  if (preferred) return preferred
  return options
    .filter((option) => option.duration_s != null && Number.isFinite(option.duration_s) && option.duration_s >= 0)
    .sort((a, b) => a.duration_s! - b.duration_s!)[0]
}

function mapFactCost(place: PlaceFact, sourcesById: Map<string, { provider: string; queried_at: string }>): PreviewCostView {
  const amount = formatReferenceAmount(place.cost?.value)
  const sourceId = place.field_sources?.cost ?? place.source_refs?.[0]
  const source = sourceId ? sourcesById.get(sourceId) : undefined
  const sourceLabel = source?.provider === 'amap' ? '高德地图' : source?.provider ?? null
  const queriedAt = source?.queried_at ?? null
  const evidenceLabel = formatCostEvidence(sourceLabel, queriedAt)
  return amount == null
    ? { status: 'unknown', amount: null, text: '未查询到可供参考的人均消费', sourceLabel, queriedAt, evidenceLabel }
    : { status: 'known', amount, text: `¥${amount} / 人`, sourceLabel, queriedAt, evidenceLabel }
}

function endpointLabel(
  leg: RouteLeg,
  side: 'from' | 'to',
  activities: DraftActivity[],
  placesByFact: Map<string, PlaceFact>,
  placesByPoi: Map<string, PlaceFact>,
): string {
  const endpoint = side === 'from' ? leg.from_endpoint : leg.to_endpoint
  if (endpoint.kind === 'activity') {
    const activity = activities.find((item) => item.activity_id === endpoint.reference_id)
    if (activity?.kind === 'free_time') return '自由活动区域'
    const place = activity?.place_fact_id ? placesByFact.get(activity.place_fact_id) : undefined
    return place?.name ?? '地点信息未核实'
  }
  const place = placesByPoi.get(endpoint.poi_id)
  if (place) return place.name
  return endpoint.kind === 'lodging' ? '住宿地点' : endpoint.kind === 'arrival' ? '到达地点' : '离开地点'
}

function coordinates(points: Array<{ lng: number; lat: number }>): GeoPoint[] {
  return points
    .filter((point) => Number.isFinite(point.lng) && Number.isFinite(point.lat))
    .map((point) => [point.lng, point.lat])
}

function toGeoPoint(point: { lng: number; lat: number } | null | undefined): GeoPoint | null {
  if (!point || !Number.isFinite(point.lng) || !Number.isFinite(point.lat)) return null
  return [point.lng, point.lat]
}

function formatCostEvidence(sourceLabel: string | null, queriedAt: string | null): string | null {
  if (!sourceLabel && !queriedAt) return null
  const time = formatPreviewEvidenceTime(queriedAt)
  return [sourceLabel ? `来源：${sourceLabel}` : null, time ? `查询于 ${time}` : null].filter(Boolean).join(' · ')
}

function mapValidationIssue(issue: ValidationIssue): ValidationIssue {
  let message = issue.message
  switch (issue.code) {
    case 'route_unverified':
      message = issue.message.includes('missing_route_leg')
        ? '尚未查到该路段耗时。'
        : '该路段耗时尚未核实。'
      break
    case 'conditional_slot_unresolved':
      message = '该时段是否需要安排地点仍待确认。'
      break
  }
  return { ...issue, message: cleanTechnicalTerms(message) }
}

function cleanTechnicalTerms(message: string): string {
  return message
    .replace(/\bF07\b/g, '离开日可用时间计算')
    .replace(/\bmissing_route_leg\b/g, '该路段耗时尚未查到')
}

function routeModeStatus(status: string | undefined): string {
  if (status === 'available') return '路线已查询'
  if (status === 'pending') return '查询中'
  if (status === 'empty') return '未查到可用路线'
  if (status === 'error') return '路线查询失败'
  return '未查到路线信息'
}

function isMealPeriod(period: V4SlotPeriod): boolean {
  return period === 'breakfast' || period === 'lunch' || period === 'dinner'
}
