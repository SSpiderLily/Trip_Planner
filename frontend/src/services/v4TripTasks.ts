import axios from 'axios'
import type { V4TripAccepted, V4TripRequest } from '@/types/v4TripRequest'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

// This client has no request/response logging because accepted responses contain a bearer token.
const v4TaskClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
})

export async function submitV4TripTask(request: V4TripRequest): Promise<V4TripAccepted> {
  const response = await v4TaskClient.post('/api/trip/v4/tasks', request)
  const accepted = response.data as Partial<V4TripAccepted>
  if (
    !accepted.task_id?.trim()
    || !accepted.plan_id?.trim()
    || !accepted.access_token?.trim()
    || accepted.status !== 'accepted'
  ) {
    throw new Error('服务已响应，但没有返回完整的预览访问信息。请勿重复提交。')
  }
  return accepted as V4TripAccepted
}


export interface HotelOption {
  poi_id: string
  name: string
  address: string | null
  city: string
  location: { lng: number; lat: number }
}

export async function searchHotels(city: string, keywords: string): Promise<HotelOption[]> {
  const response = await v4TaskClient.get('/api/poi/hotels', { params: { city, keywords } })
  return response.data.items
}
