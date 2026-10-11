import type { V4PreviewEnvelope } from '@/types/v4Preview'
import type { TripPreviewView } from '@/utils/v4PreviewMapper'

export interface V4ExportVisibleState {
  envelope: V4PreviewEnvelope
  view: TripPreviewView
}

export interface V4ExportPersistenceSource {
  taskId: string
  planId: string
  storeVersion: number
  planRevision: number
}

export interface V4PreviewExportSnapshot {
  /** Exact view captured before rendering. Future editors can supply their current derived view here. */
  visible: V4ExportVisibleState
  /** Null while local edits or a save are pending, so export cannot persist an older server snapshot. */
  persistence: V4ExportPersistenceSource | null
}

export interface V4ExportPageState {
  dirty: boolean
  saving: boolean
}

export interface V4ExportFile<T> {
  filename: string
  blob: T
}

export type V4ExportSaveResult<R> =
  | { status: 'saved'; response: R; fileCount: number }
  | { status: 'stale'; response: R; fileCount: number }
  | { status: 'skipped'; fileCount: number }
  | { status: 'failed'; error: unknown; fileCount: number }

/** Copy the visible state synchronously; no later page change can alter an in-progress export. */
export function captureV4PreviewExport(
  envelope: V4PreviewEnvelope,
  view: TripPreviewView,
  pageState: V4ExportPageState,
): V4PreviewExportSnapshot {
  const copiedEnvelope = cloneJson(envelope)
  const copiedView = cloneJson(view)
  const visible = deepFreeze({ envelope: copiedEnvelope, view: copiedView })
  const persistence = pageState.dirty || pageState.saving
    ? null
    : Object.freeze({
        taskId: copiedEnvelope.task_id,
        planId: copiedEnvelope.plan_id,
        storeVersion: copiedEnvelope.store_version,
        planRevision: copiedEnvelope.plan_revision,
      })
  return Object.freeze({ visible, persistence })
}

/**
 * Generate every file first, then initiate every download, then try one best-effort server save.
 * Rendering or download failures never submit a save request.
 */
export async function exportV4Snapshot<T, R>(args: {
  snapshot: V4PreviewExportSnapshot
  renderAll: (visible: V4ExportVisibleState) => Promise<readonly V4ExportFile<T>[]>
  startDownloads: (files: readonly V4ExportFile<T>[]) => void
  saveFinal: (source: V4ExportPersistenceSource) => Promise<R>
  canPersist?: (source: V4ExportPersistenceSource) => boolean
  onDownloadsStarted?: (willAttemptSave: boolean) => void
  isSaved: (response: R) => boolean
}): Promise<V4ExportSaveResult<R>> {
  const files = await args.renderAll(args.snapshot.visible)
  if (files.length === 0) throw new Error('没有可导出的行程图片。')
  args.startDownloads(files)
  const willAttemptSave = Boolean(
    args.snapshot.persistence
    && (!args.canPersist || args.canPersist(args.snapshot.persistence)),
  )
  args.onDownloadsStarted?.(willAttemptSave)
  if (!willAttemptSave || !args.snapshot.persistence) {
    return { status: 'skipped', fileCount: files.length }
  }
  try {
    const response = await args.saveFinal(args.snapshot.persistence)
    return args.isSaved(response)
      ? { status: 'saved', response, fileCount: files.length }
      : { status: 'stale', response, fileCount: files.length }
  } catch (error) {
    return { status: 'failed', error, fileCount: files.length }
  }
}

export function canApplyV4Restore(args: {
  dirty: boolean
  saving: boolean
  current: Pick<V4PreviewEnvelope, 'task_id' | 'plan_id' | 'store_version' | 'plan_revision'>
  expected: Pick<V4PreviewEnvelope, 'task_id' | 'plan_id' | 'store_version' | 'plan_revision'>
}): boolean {
  return !args.dirty && !args.saving
    && args.current.task_id === args.expected.task_id
    && args.current.plan_id === args.expected.plan_id
    && args.current.store_version === args.expected.store_version
    && args.current.plan_revision === args.expected.plan_revision
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function deepFreeze<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value)
    for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child)
  }
  return value
}
