import type { Activity, Cost, DayEditOperation, PoiSearchResult, RouteDay, Summary, TravelMode } from '@/types/itinerary'

export interface PendingDayEdit {
  operation: DayEditOperation
  optimisticActivity?: Activity
}

export function copyDay(day: RouteDay): RouteDay {
  return JSON.parse(JSON.stringify(day)) as RouteDay
}

export function makeOptimisticActivity(poi: PoiSearchResult, activityId: string): Activity {
  return {
    activity_id: activityId,
    type: 'sightseeing',
    period: '待安排',
    title: poi.name,
    description: '正在根据当天路线安排位置与时间。',
    duration_minutes: null,
    place: {
      source: 'amap',
      source_id: poi.id,
      name: poi.name,
      address: poi.address || '',
      longitude: poi.location.longitude,
      latitude: poi.location.latitude,
      is_area_reference: false
    },
    estimated_cost: { amount: null, basis: '未查询到真实数据' },
    requirement_ids: [],
    start_at: null,
    end_at: null
  }
}

/** Apply only the visible list/selection part of an edit. Times and route estimates
 * remain hidden by the caller until the server recalculates the day. */
export function applyOptimisticEdit(day: RouteDay, edit: PendingDayEdit): RouteDay {
  const next = copyDay(day)
  const operation = edit.operation
  if (operation.type === 'add_poi' && edit.optimisticActivity) {
    if (!next.activities.some(item => item.activity_id === operation.client_activity_id)) {
      next.activities.push(edit.optimisticActivity)
    }
    return next
  }
  if (operation.type === 'delete_activity') {
    next.activities = next.activities.filter(item => item.activity_id !== operation.activity_id)
    next.legs = next.legs.filter(leg => leg.from_activity_id !== operation.activity_id && leg.to_activity_id !== operation.activity_id)
    return next
  }
  if (operation.type === 'move_activity') {
    const index = next.activities.findIndex(item => item.activity_id === operation.activity_id)
    const target = index + (operation.direction === 'up' ? -1 : 1)
    if (index >= 0 && target >= 0 && target < next.activities.length) {
      const [activity] = next.activities.splice(index, 1)
      next.activities.splice(target, 0, activity)
    }
    return next
  }
  if (operation.type === 'select_leg_mode') {
    const leg = next.legs.find(item => item.from_activity_id === operation.from_activity_id && item.to_activity_id === operation.to_activity_id)
    if (leg) {
      leg.selected_mode = operation.mode
      leg.mode = operation.mode
      leg.selection_source = 'manual'
    }
  }
  return next
}

export function applyOptimisticEdits(day: RouteDay, edits: PendingDayEdit[]): RouteDay {
  return edits.reduce((current, edit) => applyOptimisticEdit(current, edit), day)
}

export function isRouteOptionAvailable(option: { duration_minutes: number | null; status: string } | undefined): boolean {
  return !!option && option.status === 'available' && option.duration_minutes !== null && option.duration_minutes >= 0
}

export const TRAVEL_MODE_ORDER: TravelMode[] = ['walking', 'bicycling', 'transit', 'driving']

export function aggregateSummaries(summaries: Summary[]): Summary {
  return {
    known_total: summaries.reduce((total, summary) => total + (Number.isFinite(summary.known_total) ? summary.known_total : 0), 0),
    unknown_count: summaries.reduce((total, summary) => total + Math.max(0, summary.unknown_count || 0), 0),
    complete: summaries.length > 0 && summaries.every(summary => summary.complete),
    basis: '按当前各日已查询费用汇总'
  }
}

export function visibleCost(cost: Cost | undefined): string {
  if (!cost || cost.amount === null || !Number.isFinite(cost.amount)) return '未查询到真实数据'
  return `参考 ¥${cost.amount}`
}
