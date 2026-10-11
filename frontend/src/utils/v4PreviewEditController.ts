import type { V4EditOperation, V4EditRequest, V4PreviewAccess, V4PreviewEnvelope } from '@/types/v4Preview'

const PENDING_EDIT_PREFIX = 'trip_planner_v4_pending_edit_v1:'
const MAX_EDIT_OPERATIONS = 20

export interface V4PendingEdit {
  taskId: string
  planId: string
  summary: string
  details: string[]
  request: V4EditRequest
  status: 'sending' | 'retry' | 'conflict'
  conflictMessage?: string
  createdAt: string
}

export function createV4PendingEdit(args: {
  envelope: V4PreviewEnvelope
  commandId: string
  operations: V4EditOperation[]
  summary: string
  details?: string[]
}): V4PendingEdit {
  const { envelope, commandId, operations } = args
  const dayIds = new Set(operations.map((operation) => operation.day_id))
  if (operations.length === 0 || operations.length > MAX_EDIT_OPERATIONS) {
    throw new Error(`一次编辑最多包含 ${MAX_EDIT_OPERATIONS} 项操作。`)
  }
  if (dayIds.size !== 1 || !envelope.preview.draft.days.some((day) => day.day_id === [...dayIds][0])) {
    throw new Error('一次只能编辑同一天的行程。')
  }
  if (envelope.plan_id !== envelope.preview.draft.plan_id
    || envelope.plan_revision !== envelope.preview.draft.revision) {
    throw new Error('当前行程版本信息不一致，请刷新后重试。')
  }
  const command = {
    plan_id: envelope.plan_id,
    day_id: [...dayIds][0],
    base_revision: envelope.plan_revision,
    command_id: commandId,
    operations: cloneJson(operations),
  }
  return {
    taskId: envelope.task_id,
    planId: envelope.plan_id,
    summary: args.summary,
    details: args.details ?? [],
    request: {
      command,
      expected_store_version: envelope.store_version,
    },
    status: 'sending',
    createdAt: new Date().toISOString(),
  }
}

export function markV4PendingEdit(
  pending: V4PendingEdit,
  status: V4PendingEdit['status'],
  conflictMessage?: string,
): V4PendingEdit {
  const updated = { ...pending, status }
  if (conflictMessage) updated.conflictMessage = conflictMessage
  else delete updated.conflictMessage
  return updated
}

/** 用户明确点击重试后，按最新版本重新生成 CAS；不会自动调用。 */
export function rebaseV4PendingEdit(
  pending: V4PendingEdit,
  envelope: V4PreviewEnvelope,
  commandId: string,
): V4PendingEdit {
  if (pending.taskId !== envelope.task_id || pending.planId !== envelope.plan_id) {
    throw new Error('这项编辑属于另一份行程，不能重试。')
  }
  if (pending.request.command.day_id !== pending.request.command.operations[0]?.day_id) {
    throw new Error('这项编辑的日期信息不完整，不能重试。')
  }
  const dayId = pending.request.command.day_id
  const currentDay = envelope.preview.draft.days.find((day) => day.day_id === dayId)
  if (!currentDay) throw new Error('原编辑日期已不存在，不能重试。')
  const currentIds = currentDay.activities.map((activity) => activity.activity_id)
  const currentIdSet = new Set(currentIds)
  const addedClientRefs = new Set(pending.request.command.operations
    .filter((operation) => operation.type === 'add_activity')
    .map((operation) => operation.client_ref))
  const operations = pending.request.command.operations.flatMap((operation): V4EditOperation[] => {
    if (operation.type === 'remove_activity') {
      return currentIdSet.has(operation.activity_id) ? [{ ...operation }] : []
    }
    if (operation.type === 'reorder_activities') {
      const requested = operation.activity_ids.filter((id) => currentIdSet.has(id) || addedClientRefs.has(id))
      const requestedSet = new Set(requested)
      const newlyPresent = currentIds.filter((id) => !requestedSet.has(id))
      return [{ ...operation, activity_ids: [...requested, ...newlyPresent] }]
    }
    return [{ ...operation }]
  })
  if (!operations.length) throw new Error('原编辑内容已在最新行程中完成，无需重试。')
  const rebased = createV4PendingEdit({
    envelope,
    commandId,
    operations,
    summary: pending.summary,
    details: pending.details,
  })
  return rebased
}

export function persistV4PendingEdit(pending: V4PendingEdit): void {
  try {
    localStorage.setItem(pendingEditKey(pending.taskId), JSON.stringify(pending))
  } catch {
    throw new Error('本机无法保留这项编辑，操作尚未发送。请检查浏览器存储空间后重试。')
  }
}

export function readV4PendingEdit(taskId: string): V4PendingEdit | null {
  let serialized: string | null
  try {
    serialized = localStorage.getItem(pendingEditKey(taskId))
  } catch {
    return null
  }
  if (!serialized) return null
  try {
    const value: unknown = JSON.parse(serialized)
    if (!isPendingEditRecord(value, taskId)) return null
    return value
  } catch {
    return null
  }
}

export function hasStoredV4PendingEdit(taskId: string): boolean {
  try { return localStorage.getItem(pendingEditKey(taskId)) !== null } catch { return false }
}

export function clearV4PendingEdit(taskId: string): boolean {
  try {
    localStorage.removeItem(pendingEditKey(taskId))
    return localStorage.getItem(pendingEditKey(taskId)) === null
  } catch {
    return false
  }
}

export function isV4EnvelopeAtLeastAsNew(
  next: Pick<V4PreviewEnvelope, 'task_id' | 'plan_id' | 'store_version' | 'plan_revision'>,
  current: Pick<V4PreviewEnvelope, 'task_id' | 'plan_id' | 'store_version' | 'plan_revision'> | null,
): boolean {
  if (!current) return true
  return next.task_id === current.task_id && next.plan_id === current.plan_id
    && next.store_version >= current.store_version
    && next.plan_revision >= current.plan_revision
}

/** A discard is safe only after the page can adopt a matching, non-older server snapshot. */
export function canAdoptV4DiscardRefresh(
  access: V4PreviewAccess,
  latest: V4PreviewEnvelope,
  current: V4PreviewEnvelope | null,
): boolean {
  return latest.task_id === access.taskId
    && latest.plan_id === access.planId
    && isV4EnvelopeAtLeastAsNew(latest, current)
}

/** Read and show server state before removing a possibly already-committed command. */
export async function refreshBeforeDiscardV4PendingEdit(args: {
  access: V4PreviewAccess
  current: V4PreviewEnvelope | null
  readLatest: () => Promise<V4PreviewEnvelope>
  adopt: (latest: V4PreviewEnvelope) => void
  clear: () => boolean
}): Promise<'discarded' | 'stale' | 'cache_failed'> {
  const latest = await args.readLatest()
  if (!canAdoptV4DiscardRefresh(args.access, latest, args.current)) return 'stale'
  args.adopt(latest)
  return args.clear() ? 'discarded' : 'cache_failed'
}

/** Route jobs can be followed only for calculations created by edit/restore commands. */
export function pendingV4RouteJobId(envelope: V4PreviewEnvelope): string | null {
  const calculation = envelope.preview.calculation
  if (calculation.current_routes !== 'pending') return null
  if (!calculation.calculation_id.startsWith('edit:')
    && !calculation.calculation_id.startsWith('restore:')) return null
  return calculation.calculation_id
}

/** Follow a superseding response only when it is current and names a different pending route job. */
export function nextV4RouteJobAfterSuperseded(
  currentCalculationId: string,
  latest: V4PreviewEnvelope,
  current: V4PreviewEnvelope,
): string | null {
  if (!isV4EnvelopeAtLeastAsNew(latest, current)) return null
  const nextCalculationId = pendingV4RouteJobId(latest)
  return nextCalculationId && nextCalculationId !== currentCalculationId ? nextCalculationId : null
}

export function pendingV4EditSummary(pending: V4PendingEdit): string {
  return pending.status === 'conflict'
    ? `待确认的编辑：${pending.summary}`
    : `待同步的编辑：${pending.summary}`
}

function pendingEditKey(taskId: string): string {
  return `${PENDING_EDIT_PREFIX}${encodeURIComponent(taskId)}`
}

function isPendingEditRecord(value: unknown, taskId: string): value is V4PendingEdit {
  if (!isRecord(value) || value.taskId !== taskId || typeof value.planId !== 'string'
    || typeof value.summary !== 'string' || !Array.isArray(value.details)
    || !value.details.every((detail) => typeof detail === 'string')
    || typeof value.createdAt !== 'string'
    || !['sending', 'retry', 'conflict'].includes(String(value.status))) return false
  if (!isRecord(value.request) || !Number.isInteger(value.request.expected_store_version)
    || Number(value.request.expected_store_version) < 1 || !isRecord(value.request.command)) return false
  const command = value.request.command
  if (command.plan_id !== value.planId || typeof command.day_id !== 'string'
    || !command.day_id || !Number.isInteger(command.base_revision) || Number(command.base_revision) < 1
    || typeof command.command_id !== 'string' || !command.command_id
    || !Array.isArray(command.operations) || command.operations.length < 1
    || command.operations.length > MAX_EDIT_OPERATIONS) return false
  return command.operations.every((operation) => (
    isRecord(operation)
    && operation.day_id === command.day_id
    && (operation.type === 'add_activity'
      ? nonEmpty(operation.client_ref) && nonEmpty(operation.candidate_ref) && nonEmpty(operation.slot_id)
      : operation.type === 'remove_activity'
        ? nonEmpty(operation.activity_id)
        : operation.type === 'reorder_activities'
          ? Array.isArray(operation.activity_ids) && operation.activity_ids.every(nonEmpty)
          : operation.type === 'select_leg_mode'
            ? nonEmpty(operation.leg_id) && nonEmpty(operation.option_id)
              && ['transit', 'bicycling', 'walking'].includes(String(operation.mode))
            : false)
  ))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
