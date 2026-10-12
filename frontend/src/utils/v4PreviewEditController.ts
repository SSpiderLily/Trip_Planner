import type {
  V4DestinationCategory,
  V4DestinationSelectionRequest,
  V4EditOperation,
  V4EditRequest,
  V4PreviewAccess,
  V4PreviewEnvelope,
} from '@/types/v4Preview'

const PENDING_EDIT_PREFIX = 'trip_planner_v4_pending_edit_v1:'
const MAX_EDIT_OPERATIONS = 20

interface V4PendingEditBase {
  taskId: string
  planId: string
  summary: string
  details: string[]
  status: 'sending' | 'retry' | 'conflict'
  conflictMessage?: string
  createdAt: string
}

export interface V4PendingCommandEdit extends V4PendingEditBase {
  kind: 'edit'
  request: V4EditRequest
}

export interface V4PendingDestinationSelection extends V4PendingEditBase {
  kind: 'destination_selection'
  category: V4DestinationCategory
  request: V4DestinationSelectionRequest
}

export type V4PendingEdit = V4PendingCommandEdit | V4PendingDestinationSelection

export function createV4PendingEdit(args: {
  envelope: V4PreviewEnvelope
  commandId: string
  operations: V4EditOperation[]
  summary: string
  details?: string[]
}): V4PendingCommandEdit {
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
    kind: 'edit',
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

export function createV4PendingDestinationSelection(args: {
  envelope: V4PreviewEnvelope
  candidateToken: string
  category: V4DestinationCategory
  dayId: string
  slotId: string
  commandId: string
  clientRef: string
  name: string
  slotLabel: string
}): V4PendingDestinationSelection {
  const { envelope } = args
  if (envelope.plan_id !== envelope.preview.draft.plan_id
    || envelope.plan_revision !== envelope.preview.draft.revision) {
    throw new Error('当前行程版本信息不一致，请刷新后重试。')
  }
  if (!nonEmpty(args.candidateToken) || args.candidateToken.length < 32 || args.candidateToken.length > 128) {
    throw new Error('搜索结果已失效，请重新搜索后再选择。')
  }
  if (!nonEmpty(args.commandId) || !nonEmpty(args.clientRef)) {
    throw new Error('无法保留这项选择，请重试。')
  }
  const day = envelope.preview.planning_input.days.find((item) => item.day_id === args.dayId)
  const slot = day?.slots.find((item) => item.slot_id === args.slotId)
  if (!slot || slot.eligibility === 'skipped'
    || (args.category === 'meal' && !['breakfast', 'lunch', 'dinner'].includes(slot.period))
    || (args.category === 'sightseeing' && !['morning', 'afternoon', 'evening'].includes(slot.period))) {
    throw new Error('所选时段不适合安排这个地点，请重新选择时段。')
  }
  return {
    kind: 'destination_selection',
    category: args.category,
    taskId: envelope.task_id,
    planId: envelope.plan_id,
    summary: `将“${args.name}”加入${args.slotLabel}`,
    details: [
      `类别：${args.category === 'meal' ? '餐馆' : '景点'}`,
      `时段：${args.slotLabel}`,
      '地点由当前城市的搜索结果提供。',
    ],
    request: {
      candidate_token: args.candidateToken,
      day_id: args.dayId,
      slot_id: args.slotId,
      expected_store_version: envelope.store_version,
      expected_plan_revision: envelope.plan_revision,
      command_id: args.commandId,
      client_ref: args.clientRef,
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
  if (pending.kind === 'destination_selection') {
    const day = envelope.preview.planning_input.days.find((item) => item.day_id === pending.request.day_id)
    const slot = day?.slots.find((item) => item.slot_id === pending.request.slot_id)
    const allowed = pending.category === 'meal'
      ? ['breakfast', 'lunch', 'dinner']
      : ['morning', 'afternoon', 'evening']
    if (!slot || slot.eligibility === 'skipped' || !allowed.includes(slot.period)) {
      throw new Error('原选择时段已不可用，不能重试。')
    }
    return {
      ...pending,
      request: {
        ...pending.request,
        expected_store_version: envelope.store_version,
        expected_plan_revision: envelope.plan_revision,
        command_id: commandId,
      },
      status: 'sending',
      conflictMessage: undefined,
    }
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
    if (!isRecord(value)) return null
    // Older cached edit requests predate the `kind` discriminator.
    const normalized = value.kind === undefined ? { ...value, kind: 'edit' } : value
    if (!isPendingEditRecord(normalized, taskId)) return null
    return normalized
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

/** Follow durable adjacent-route jobs from generation, edit, or restore. */
export function pendingV4RouteJobId(envelope: V4PreviewEnvelope): string | null {
  const calculation = envelope.preview.calculation
  if (calculation.current_routes !== 'pending') return null
  if (!calculation.calculation_id.startsWith('edit:')
    && !calculation.calculation_id.startsWith('restore:')
    && !calculation.calculation_id.startsWith('generation:')) return null
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
  if (!isRecord(value.request)) return false
  if (value.kind === 'destination_selection') {
    return (value.category === 'sightseeing' || value.category === 'meal')
      && nonEmpty(value.request.candidate_token)
      && value.request.candidate_token.length >= 32
      && value.request.candidate_token.length <= 128
      && nonEmpty(value.request.day_id)
      && nonEmpty(value.request.slot_id)
      && Number.isInteger(value.request.expected_store_version)
      && Number(value.request.expected_store_version) > 0
      && Number.isInteger(value.request.expected_plan_revision)
      && Number(value.request.expected_plan_revision) > 0
      && nonEmpty(value.request.command_id)
      && nonEmpty(value.request.client_ref)
  }
  if (value.kind !== 'edit' || !Number.isInteger(value.request.expected_store_version)
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
