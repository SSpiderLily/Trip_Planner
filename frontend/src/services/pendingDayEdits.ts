import type { DayEditOperation } from '@/types/itinerary'
import type { PendingDayEdit } from './dayEditing'

function touchesActivity(operation: DayEditOperation, activityId: string): boolean {
  if (operation.type === 'add_poi') return operation.client_activity_id === activityId
  if (operation.type === 'delete_activity' || operation.type === 'move_activity') return operation.activity_id === activityId
  return operation.from_activity_id === activityId || operation.to_activity_id === activityId
}

function applyActivityOperations(baseOrder: string[], edits: PendingDayEdit[]): string[] {
  const order = [...baseOrder]
  for (const { operation } of edits) {
    if (operation.type === 'add_poi') {
      if (!order.includes(operation.client_activity_id)) order.push(operation.client_activity_id)
    } else if (operation.type === 'delete_activity') {
      const index = order.indexOf(operation.activity_id)
      if (index >= 0) order.splice(index, 1)
    } else if (operation.type === 'move_activity') {
      const index = order.indexOf(operation.activity_id)
      const target = index + (operation.direction === 'up' ? -1 : 1)
      if (index >= 0 && target >= 0 && target < order.length) [order[index], order[target]] = [order[target], order[index]]
    }
  }
  return order
}

/** Remove not-yet-sent additions that were deleted again, then preserve the visible order. */
export function compactCancelledPendingAdds(edits: PendingDayEdit[], baseOrder: string[]): { edits: PendingDayEdit[]; cancelledActivityIds: string[] } {
  const queuedAdds = new Set<string>()
  const cancelled = new Set<string>()
  for (const { operation } of edits) {
    if (operation.type === 'add_poi') queuedAdds.add(operation.client_activity_id)
    if (operation.type === 'delete_activity' && queuedAdds.has(operation.activity_id)) cancelled.add(operation.activity_id)
  }
  if (!cancelled.size) return { edits, cancelledActivityIds: [] }
  const targetOrder = applyActivityOperations(baseOrder, edits)
  const compacted = edits.filter(({ operation }) => ![...cancelled].some(id => touchesActivity(operation, id)))
  const workingOrder = applyActivityOperations(baseOrder, compacted)
  const corrections = activityOrderMoves(workingOrder, targetOrder).map(operation => ({ operation }))
  return { edits: [...compacted, ...corrections], cancelledActivityIds: [...cancelled] }
}

/** Create only the moves needed to make an activity list match the desired order. */
export function activityOrderMoves(currentIds: string[], desiredIds: string[]): DayEditOperation[] {
  const working = [...currentIds]
  const target = desiredIds.filter(id => working.includes(id))
  const operations: DayEditOperation[] = []
  target.forEach((id, targetIndex) => {
    let index = working.indexOf(id)
    while (index > targetIndex) {
      operations.push({ type: 'move_activity', activity_id: id, direction: 'up' })
      ;[working[index - 1], working[index]] = [working[index], working[index - 1]]
      index--
    }
    while (index >= 0 && index < targetIndex) {
      operations.push({ type: 'move_activity', activity_id: id, direction: 'down' })
      ;[working[index], working[index + 1]] = [working[index + 1], working[index]]
      index++
    }
  })
  return operations
}
