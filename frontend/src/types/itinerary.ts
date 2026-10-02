export interface Place {
  source: 'amap'; source_id: string; name: string; address: string
  longitude: number; latitude: number; is_area_reference: boolean
}
export interface Cost { amount: number | null; basis: string }
export interface Summary { known_total: number; unknown_count: number; complete: boolean; basis?: string }
export interface Activity {
  activity_id: string; type: 'sightseeing' | 'meal' | 'free_time'; period: string
  title: string; description: string; duration_minutes: number | null; place: Place | null
  estimated_cost: Cost; requirement_ids: string[]
}
export interface Leg {
  leg_id: string; from_activity_id: string; to_activity_id: string
  origin: Place | null; destination: Place | null; mode: string
  duration_minutes: number | null; distance_meters: number | null
  data_basis: string; estimated_cost: Cost; note: string
}
export interface Issue {
  issue_id: string; code: string; category: string; date: string | null
  activity_id: string | null; leg_id: string | null; message: string
}
export interface RouteDay {
  date: string; description: string; activities: Activity[]; legs: Leg[]
  weather: { dayweather: string; nightweather: string; daytemp: string; nighttemp: string } | null
  time_summary: Summary & { status: string; buffer_minutes: number; budget_minutes: number }
  cost_summary: Summary; optional_plans: unknown[]
}
export interface RouteItinerary {
  schema_version: 2
  planning_conditions: {
    city: string; start_date: string; end_date: string; remarks: string
    transportation: string; budget_per_adult: number | null
    interpretation_notes: string[]; reminder_only_requests: string[]
  }
  lodging_base: { source: string; user_input: string | null; area_name: string | null; recommendation_reason: string | null; place: Place | null }
  days: RouteDay[]; cost_summary: Summary; issues: Issue[]
}
