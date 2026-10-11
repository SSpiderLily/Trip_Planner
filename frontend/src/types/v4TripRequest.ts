/** v4 原始请求与异步任务接受回执。 */
export interface V4TripRequest {
  schema_version: 4
  request_id: string
  city: string
  arrival_at: string
  departure_at: string
  timezone: 'Asia/Shanghai'
  preferences: string[]
  transport_preferences: Array<'transit' | 'bicycling' | 'walking'>
  arrival_place_query: string | null
  departure_place_query: string | null
  lodging_query: string | null
  explicit_return_buffer_s: number | null
  budget: {
    amount: number | null
    currency: 'CNY'
    scope: 'whole_trip'
    party_basis: 'unspecified'
    party_size: null
  }
  remarks: string
}

export interface V4TripAccepted {
  task_id: string
  plan_id: string
  access_token: string
  status: 'accepted'
}
