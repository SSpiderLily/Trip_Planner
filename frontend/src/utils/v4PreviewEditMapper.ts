import type { PreviewActivityView } from '@/utils/v4PreviewMapper'

export interface V4DestinationGroup {
  key: string
  poiId: string | null
  kind: 'sightseeing' | 'meal'
  name: string
  address: string
  area: string
  coordinate: PreviewActivityView['coordinate']
  candidateRef: string | null
  activityIds: string[]
  periodLabels: string[]
}

/** 地图下按 POI 去重；同一地点在不同时段的活动仍分别保留，供批量删除。 */
export function groupV4DestinationActivities(activities: readonly PreviewActivityView[]): V4DestinationGroup[] {
  const groups = new Map<string, V4DestinationGroup>()
  for (const activity of activities) {
    if (activity.kind === 'free_time') continue
    const key = activity.poiId ? `poi:${activity.poiId}` : `activity:${activity.id}`
    const existing = groups.get(key)
    if (existing) {
      existing.activityIds.push(activity.id)
      if (!existing.periodLabels.includes(activity.periodLabel)) existing.periodLabels.push(activity.periodLabel)
      if (!existing.candidateRef && activity.candidateRef) existing.candidateRef = activity.candidateRef
      if (!existing.coordinate && activity.coordinate) existing.coordinate = activity.coordinate
      if (!existing.address && activity.address) existing.address = activity.address
      if (!existing.area && activity.area) existing.area = activity.area
      continue
    }
    groups.set(key, {
      key,
      poiId: activity.poiId,
      kind: activity.kind === 'meal' ? 'meal' : 'sightseeing',
      name: activity.title,
      address: activity.address,
      area: activity.area,
      coordinate: activity.coordinate,
      candidateRef: activity.candidateRef,
      activityIds: [activity.id],
      periodLabels: [activity.periodLabel],
    })
  }
  return [...groups.values()]
}
