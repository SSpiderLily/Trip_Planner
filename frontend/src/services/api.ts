import axios from 'axios'
import type { TripFormData, TripPlanResponse, TaskStatus, SpanSummary, SpanDetail } from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000, // 2分钟超时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

export async function submitTask(data: TripFormData): Promise<{ task_id: string; status: string }> {
  return (await apiClient.post('/api/trip/tasks', data)).data
}
export async function getTask(id: string): Promise<TaskStatus> {
  return (await apiClient.get(`/api/trip/tasks/${encodeURIComponent(id)}`)).data
}
export async function getTaskResult(id: string): Promise<TripPlanResponse> {
  return (await apiClient.get(`/api/trip/tasks/${encodeURIComponent(id)}/result`)).data
}
export async function listTasks(status = '', offset = 0): Promise<TaskStatus[]> {
  return (await apiClient.get('/api/trip/tasks', { params: { status: status || undefined, limit: 50, offset } })).data
}
export async function getSpans(id: string): Promise<SpanSummary[]> {
  return (await apiClient.get(`/api/trip/tasks/${encodeURIComponent(id)}/spans`)).data
}
export async function getSpan(id: string, spanId: string): Promise<SpanDetail> {
  return (await apiClient.get(`/api/trip/tasks/${encodeURIComponent(id)}/spans/${encodeURIComponent(spanId)}`)).data
}
export function errorText(error: any): string {
  const detail = error.response?.data?.detail
  if (Array.isArray(detail)) return detail.map((item: any) => item.msg).join('；')
  return detail?.message || (typeof detail === 'string' ? detail : error.message) || '请求失败'
}
export const stepName = (name: string) => ({
  planning: '规划执行', 'planning.collect_sights': '搜集景点候选', 'planning.collect_support': '搜集住宿与餐饮',
  'agent.collect_sights': '理解需求与搜集景点', 'agent.collect_support': '搜集顺路住宿与餐饮',
  'agent.arrange_layout': '安排游玩分布', 'agent.arrange_final': '生成每日路线', 'agent.revision': '调整行程',
  'agent.repair_layout': '修复初稿格式', 'agent.repair_final': '修复行程格式', 'validation.itinerary_v2': '核实行程与检查路线',
  'tool.amap_maps_search_detail': '核实地点位置', 'tool.amap_maps_direction_transit_integrated_by_coordinates': '查询公交路线',
  'tool.amap_maps_direction_walking_by_coordinates': '查询步行路线', 'tool.amap_maps_direction_driving_by_coordinates': '查询驾车路线', 'planning.initialize': '初始化规划器', 'agent.attraction': '搜索景点',
  'agent.weather': '查询天气', 'agent.hotel': '搜索酒店', 'agent.planner': '整合行程',
  'llm.invoke': '模型调用', 'validation.itinerary': '校验行程',
  'tool.amap_maps_text_search': '高德地点搜索', 'tool.amap_maps_weather': '高德天气查询'
} as Record<string, string>)[name] || name
export const stateName = (state: string) => ({ accepted: '已接收', running: '执行中', succeeded: '生成完成', failed: '失败', interrupted: '已中断' } as Record<string, string>)[state] || state

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient
