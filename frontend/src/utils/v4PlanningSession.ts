/** 规划任务的读取与交付边界；供应商、地图和浏览器存储均不属于本模块。 */
import type { V4PreviewAccess, V4PreviewEnvelope, V4TaskStatus } from '@/types/v4Preview'
import { v4GenerationFailureMessage, v4PreviewStageLabel } from './v4PreviewProgress'

export class V4PlanningReadError extends Error {
  constructor(message: string, readonly action: 'read' | 'replan' = 'replan') { super(message) }
}

export function planningObservationTarget(taskId: string | null | undefined) {
  return taskId && taskId.trim() === taskId ? { path: '/observability', query: { task: taskId, view: 'trace' } } : null
}

interface PlanningReads {
  readStatus: (access: V4PreviewAccess) => Promise<V4TaskStatus>
  readPreview: (access: V4PreviewAccess) => Promise<V4PreviewEnvelope>
  isCurrent: () => boolean
  onProgress: (status: V4TaskStatus, label: string) => void
  pause: (milliseconds: number) => Promise<void>
}

function previewNotReady(error: unknown): boolean {
  const response = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } } | null)?.response
  return response?.status === 409 && response.data?.detail?.code === 'PREVIEW_NOT_READY'
}

export async function waitForV4PlanningPreview(access: V4PreviewAccess, reads: PlanningReads): Promise<V4PreviewEnvelope | null> {
  while (reads.isCurrent()) {
    try {
      const status = await reads.readStatus(access)
      if (!reads.isCurrent()) return null
      if (status.task_id !== access.taskId || status.plan_id !== access.planId) {
        throw new V4PlanningReadError('无法恢复这份预览。请从首页重新开始。')
      }
      if (typeof status.preview_ready !== 'boolean') throw new V4PlanningReadError('预览交付状态尚未同步，请重新读取。', 'read')
      if (!['accepted', 'running', 'ready', 'failed', 'interrupted'].includes(status.status)) {
        throw new V4PlanningReadError('暂时无法识别任务状态，请重新读取。', 'read')
      }
      reads.onProgress(status, v4PreviewStageLabel(status.stage, status.status))
      if (status.status === 'failed') throw new V4PlanningReadError(v4GenerationFailureMessage(status.error_code))
      if (status.status === 'interrupted') throw new V4PlanningReadError('规划任务已中断，没有可预览的行程。请返回首页重新规划。')
      // 只接受事务已发布的地点预览；交通是否完成不参与此判断。
      if (status.preview_ready) {
        try {
          const envelope = await reads.readPreview(access)
          if (!reads.isCurrent()) return null
          if (envelope.task_id !== access.taskId || envelope.plan_id !== access.planId
              || envelope.preview?.draft?.plan_id !== access.planId
              || (envelope.preview.draft.source_run_id !== undefined && envelope.preview.draft.source_run_id !== access.taskId)) {
            throw new V4PlanningReadError('无法恢复这份预览。请从首页重新开始。')
          }
          if (!Number.isSafeInteger(envelope.store_version) || envelope.store_version < 1
              || !Number.isSafeInteger(envelope.plan_revision) || envelope.plan_revision < 1
              || envelope.preview.draft.revision !== envelope.plan_revision
              || (status.store_version !== null && envelope.store_version < status.store_version)) {
            throw new V4PlanningReadError('预览版本尚未同步，请重新读取。', 'read')
          }
          return envelope
        } catch (error) {
          if (!reads.isCurrent()) return null
          if (!previewNotReady(error)) throw error
        }
      } else if (status.status === 'ready') {
        throw new V4PlanningReadError('预览交付状态尚未同步，请重新读取。', 'read')
      }
      await reads.pause(1800)
    } catch (error) {
      if (!reads.isCurrent()) return null
      throw error
    }
  }
  return null
}
