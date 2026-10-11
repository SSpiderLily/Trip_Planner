import axios from 'axios'
import { mapV4DestinationSelectFailure } from '@/utils/v4DestinationSearchMapper'
import type {
  V4FinalSaveRequest,
  V4FinalSaveResponse,
  V4FinalState,
  V4FinalRestoreRequest,
  V4EditRequest,
  V4EditResponse,
  V4EditJobResponse,
  V4DestinationSearchRequest,
  V4DestinationSearchResponse,
  V4DestinationSelectionRequest,
  V4PreviewAccess,
  V4PreviewEnvelope,
  V4RestoreResponse,
  V4TaskStatus,
} from '@/types/v4Preview'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const CURRENT_PREVIEW_KEY = 'trip_planner_v4_current_preview_v1'
const ACCESS_KEY_PREFIX = 'trip_planner_v4_preview_access_v1:'
let pendingPreviewAccess: V4PreviewAccess | null = null

// Separate from the legacy client so task credentials are never included in request logging.
const previewClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
})

export function savePreviewAccess(taskId: string, accessToken: string, planId: string): void {
  if (!taskId.trim() || !accessToken.trim() || !planId.trim()) {
    throw new Error('预览访问信息不完整，无法恢复行程。')
  }
  const access: V4PreviewAccess = { taskId, planId, accessToken }
  // Keep the access in memory first so this page can continue if browser storage is blocked.
  pendingPreviewAccess = access
  try {
    persistPreviewAccess(access)
    pendingPreviewAccess = null
  } catch {
    throw new Error('本机无法保存预览恢复信息。当前页面仍可继续查看，但刷新或关闭后无法恢复。')
  }
}

export function getLatestPreviewAccess(): V4PreviewAccess | null {
  if (pendingPreviewAccess) return { ...pendingPreviewAccess }
  const taskId = safeStorageGet(CURRENT_PREVIEW_KEY)
  return taskId ? readPreviewAccess(taskId) : null
}

/** 本页因存储不可用而暂存的访问信息；不写入日志或 URL。 */
export function getPendingPreviewAccess(): V4PreviewAccess | null {
  return pendingPreviewAccess ? { ...pendingPreviewAccess } : null
}

/** 只重试持久化最近一次访问信息，不会重新提交旅行规划。 */
export function retrySavePreviewAccess(): boolean {
  const access = pendingPreviewAccess
  if (!access) return true
  try {
    persistPreviewAccess(access)
    pendingPreviewAccess = null
    return true
  } catch {
    return false
  }
}

export function readPreviewAccess(taskId: string): V4PreviewAccess | null {
  const serialized = safeStorageGet(accessKey(taskId))
  if (!serialized) return null
  try {
    const access = JSON.parse(serialized) as Partial<V4PreviewAccess>
    if (access.taskId !== taskId || !access.planId || !access.accessToken) return null
    return access as V4PreviewAccess
  } catch {
    return null
  }
}

export async function getV4TaskStatus(access: V4PreviewAccess): Promise<V4TaskStatus> {
  const response = await previewClient.get(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}`,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4TaskStatus
}

export async function getV4Preview(access: V4PreviewAccess): Promise<V4PreviewEnvelope> {
  const response = await previewClient.get(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/preview`,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4PreviewEnvelope
}

export async function getV4FinalState(access: V4PreviewAccess): Promise<V4FinalState> {
  const response = await previewClient.get(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/final`,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4FinalState
}

export async function saveV4Final(
  access: V4PreviewAccess,
  request: V4FinalSaveRequest,
): Promise<V4FinalSaveResponse> {
  const response = await previewClient.post(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/final/save`,
    request,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4FinalSaveResponse
}

export async function restoreV4Final(
  access: V4PreviewAccess,
  request: V4FinalRestoreRequest,
): Promise<V4RestoreResponse> {
  const response = await previewClient.post(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/final/restore`,
    request,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4RestoreResponse
}

export async function submitV4Edit(
  access: V4PreviewAccess,
  request: V4EditRequest,
): Promise<V4EditResponse> {
  const response = await previewClient.post(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/edits`,
    request,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4EditResponse
}

export async function getV4EditJob(
  access: V4PreviewAccess,
  calculationId: string,
): Promise<V4EditJobResponse> {
  const response = await previewClient.get(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/edit-jobs/${encodeURIComponent(calculationId)}`,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4EditJobResponse
}

export async function searchV4Destinations(
  access: V4PreviewAccess,
  request: V4DestinationSearchRequest,
  signal?: AbortSignal,
): Promise<V4DestinationSearchResponse> {
  const response = await previewClient.post(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/destinations/search`,
    request,
    { headers: authorizationHeader(access), signal },
  )
  return response.data as V4DestinationSearchResponse
}

export async function selectV4Destination(
  access: V4PreviewAccess,
  request: V4DestinationSelectionRequest,
): Promise<V4EditResponse> {
  const response = await previewClient.post(
    `/api/trip/v4/tasks/${encodeURIComponent(access.taskId)}/destinations/select`,
    request,
    { headers: authorizationHeader(access) },
  )
  return response.data as V4EditResponse
}

export function isV4EditConflict(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 409
}

export function v4EditErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 409) return '行程已发生变化。已读取最新内容，这项编辑仍保留，请确认后重试或放弃。'
    if (error.response?.status === 422) return '这项编辑已保留，但当前安排无法应用。请调整内容后重试或放弃。'
    if (error.response?.status === 404) return '无法访问这份行程。这项编辑仍保留在本机。'
    if (error.response?.status && error.response.status >= 500) return '编辑暂未同步，具体操作已保留在本机。请重试或放弃。'
    if (error.code === 'ECONNABORTED' || !error.response) return '连接暂不可用，具体操作已保留在本机。请恢复连接后重试。'
  }
  return '编辑暂未同步，具体操作已保留在本机。请重试或放弃。'
}

export function v4DestinationSearchErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 404) return '无法访问这份行程，请返回首页重新打开。'
    if (error.response?.status === 422) return '搜索条件无效，请调整关键词后重试。'
    if (error.response?.status === 503 || (error.response?.status && error.response.status >= 500)) {
      return '地点搜索暂时不可用，请稍后重试。'
    }
    if (error.code === 'ECONNABORTED' || !error.response) return '暂时无法连接地点搜索，请检查网络后重试。'
  }
  return '地点搜索暂时失败，请稍后重试。'
}

export function v4DestinationSelectErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const code = error.response?.data?.detail?.code
      ?? error.response?.data?.code
      ?? error.response?.data?.detail?.error_code
    return mapV4DestinationSelectFailure({
      status: error.response?.status ?? null,
      code: typeof code === 'string' ? code : null,
      unavailable: error.code === 'ECONNABORTED' || !error.response,
    })
  }
  return mapV4DestinationSelectFailure({ status: null, code: null, unavailable: false })
}

export function v4FinalErrorMessage(error: unknown, action: 'save' | 'restore'): string {
  if (axios.isAxiosError(error)) {
    const code = error.response?.data?.detail?.code
    if (error.response?.status === 409) {
      if (code === 'FINAL_NOT_AVAILABLE' || code === 'FINAL_SNAPSHOT_UNAVAILABLE') {
        return '没有可还原的完整保存结果。当前行程内容保持不变。'
      }
      return action === 'restore'
        ? '行程或保存结果已有变化，当前内容保持不变。请重新查看后再试。'
        : '行程已更新，这次保存没有覆盖上次成功保存的内容。图片仍可使用。'
    }
    if (error.response?.status === 503 || (error.response?.status && error.response.status >= 500)) {
      return action === 'restore'
        ? '保存服务暂时不可用，当前行程内容保持不变。'
        : '保存服务暂时不可用，图片已导出。'
    }
    if (error.response?.status === 404) return '无法访问这份行程，当前页面内容保持不变。'
    if (error.code === 'ECONNABORTED' || !error.response) {
      return action === 'restore'
        ? '暂时无法还原，当前行程内容保持不变。'
        : '保存服务暂时无法连接，图片已导出。'
    }
  }
  return action === 'restore'
    ? '还原失败，当前行程内容保持不变。'
    : '保存未完成，图片已导出。'
}

export function v4PreviewErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'object' && detail !== null && 'code' in detail) {
      const code = String((detail as { code?: unknown }).code ?? '')
      if (code === 'PREVIEW_NOT_READY') return '行程还在准备，请稍后重试。'
    }
    if (error.response?.status === 404) return '无法恢复这份预览。请返回首页重新开始。'
    if (error.response?.status && error.response.status >= 500) return '暂时无法读取行程，请稍后重试。'
    if (error.code === 'ECONNABORTED' || !error.response) return '连接暂时不可用，请检查网络后重试。'
  }
  return '暂时无法读取行程，请稍后重试。'
}

function authorizationHeader(access: V4PreviewAccess): { Authorization: string } {
  return { Authorization: `Bearer ${access.accessToken}` }
}

function accessKey(taskId: string): string {
  return `${ACCESS_KEY_PREFIX}${encodeURIComponent(taskId)}`
}

function persistPreviewAccess(access: V4PreviewAccess): void {
  const previousTaskId = safeStorageGet(CURRENT_PREVIEW_KEY)
  safeStorageSet(accessKey(access.taskId), JSON.stringify(access))
  safeStorageSet(CURRENT_PREVIEW_KEY, access.taskId)
  if (previousTaskId && previousTaskId !== access.taskId) safeStorageRemove(accessKey(previousTaskId))
}

function safeStorageGet(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function safeStorageSet(key: string, value: string): void {
  try { localStorage.setItem(key, value) } catch {
    throw new Error('本机无法保存预览恢复信息。')
  }
}

function safeStorageRemove(key: string): void {
  try { localStorage.removeItem(key) } catch { /* A newer preview remains usable. */ }
}
