import axios from 'axios'
import type { V4PreviewAccess, V4PreviewEnvelope, V4TaskStatus } from '@/types/v4Preview'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const CURRENT_PREVIEW_KEY = 'trip_planner_v4_current_preview_v1'
const ACCESS_KEY_PREFIX = 'trip_planner_v4_preview_access_v1:'

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
  const previousTaskId = safeStorageGet(CURRENT_PREVIEW_KEY)
  const access: V4PreviewAccess = { taskId, planId, accessToken }
  safeStorageSet(accessKey(taskId), JSON.stringify(access))
  safeStorageSet(CURRENT_PREVIEW_KEY, taskId)
  if (previousTaskId && previousTaskId !== taskId) safeStorageRemove(accessKey(previousTaskId))
}

export function getLatestPreviewAccess(): V4PreviewAccess | null {
  const taskId = safeStorageGet(CURRENT_PREVIEW_KEY)
  return taskId ? readPreviewAccess(taskId) : null
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
