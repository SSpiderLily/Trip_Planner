// 类型定义

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number | null
  night_temp: number | null
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days?: number
  transportation?: string
  accommodation?: string
  lodging?: string
  preferences: string[]
  free_text_input: string
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan
}


export type TaskState = 'accepted' | 'running' | 'succeeded' | 'failed' | 'interrupted'
export interface TaskStatus {
  task_id: string
  status: TaskState
  created_at: string
  started_at: string | null
  finished_at: string | null
  interruption_detected_at: string | null
  error_code: string | null
  error_message: string | null
  error_step: string | null
  observation_incomplete: boolean
  persistence_error?: { code: string; message: string } | null
  current_step?: { span_id: string; name: string; started_at: string } | null
}
export interface SpanSummary {
  span_id: string
  task_id: string
  parent_span_id: string | null
  name: string
  operation_type: string
  status: 'running' | 'succeeded' | 'failed' | 'interrupted'
  started_at: string
  finished_at: string | null
  input_truncated: boolean
  output_truncated: boolean
}
export interface SpanDetail extends SpanSummary {
  input_data: unknown
  output_data: unknown
  error: unknown
}
