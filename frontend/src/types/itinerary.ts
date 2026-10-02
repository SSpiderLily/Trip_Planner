export interface Place {
  source: 'amap' | string
  source_id: string
  name: string
  address: string
  longitude: number
  latitude: number
  is_area_reference?: boolean
}

export interface Cost {
  amount: number | null
  basis?: string
  currency?: string
  unit?: string
  minimum?: number | null
  maximum?: number | null
  source?: string | null
}

export interface Summary {
  known_total: number
  unknown_count: number
  complete: boolean
  basis?: string
}

export interface Activity {
  activity_id: string
  type: 'sightseeing' | 'meal' | 'free_time' | string
  period: string
  title: string
  description: string
  duration_minutes: number | null
  place: Place | null
  estimated_cost?: Cost
  reference_cost?: Cost & { status?: 'available' | 'missing' | 'unknown' | string }
  requirement_ids: string[]
  start_at?: string | null
  end_at?: string | null
  user_specified?: boolean
  opening_hours?: string | null
  photos?: string[]
}

export type TravelMode = 'walking' | 'bicycling' | 'transit' | 'driving'

export interface RouteOption {
  mode: TravelMode
  duration_minutes: number | null
  distance_m?: number | null
  status: 'available' | 'no_route' | 'failed' | 'unknown' | string
  source?: string | null
  queried_at?: string | null
}

export interface Leg {
  leg_id: string
  from_activity_id: string
  to_activity_id: string
  origin: Place | null
  destination: Place | null
  /** schema v2 */
  mode?: string
  duration_minutes?: number | null
  distance_meters?: number | null
  data_basis?: string
  estimated_cost?: Cost
  note?: string
  /** schema v3 */
  options?: Partial<Record<TravelMode, RouteOption>>
  selected_mode?: TravelMode
  selection_source?: 'initial_preference' | 'fastest' | 'manual' | string
  fastest_mode?: TravelMode | null
}

export interface Issue {
  issue_id: string
  code: string
  category: string
  date: string | null
  activity_id: string | null
  leg_id: string | null
  message: string
}

export interface ActivityV2 extends Omit<Activity, 'estimated_cost'> {
  estimated_cost: Cost
}

export interface LegV2 extends Omit<Leg, 'mode' | 'duration_minutes' | 'data_basis' | 'estimated_cost' | 'note'> {
  mode: string
  duration_minutes: number | null
  data_basis: string
  estimated_cost: Cost
  note: string
}

export interface RouteDayV2 {
  date: string
  description: string
  activities: ActivityV2[]
  legs: LegV2[]
  weather: { dayweather: string; nightweather: string; daytemp: string; nighttemp: string } | null
  time_summary: Summary & { status: string; buffer_minutes: number; budget_minutes: number }
  cost_summary: Summary
  optional_plans: unknown[]
}

export interface RouteItineraryV2 {
  schema_version: 2
  planning_conditions: {
    city: string
    start_date: string
    end_date: string
    remarks: string
    transportation: string
    budget_per_adult: number | null
    interpretation_notes: string[]
    reminder_only_requests: string[]
  }
  lodging_base: { source: string; user_input: string | null; area_name: string | null; recommendation_reason: string | null; place: Place | null }
  days: RouteDayV2[]
  cost_summary: Summary
  issues: Issue[]
}

export interface RouteDay {
  day_id: string
  date: string
  description: string
  activities: Activity[]
  legs: Leg[]
  weather?: { dayweather: string; nightweather: string; daytemp: string; nighttemp: string } | null
  time_summary: {
    known_minutes: number | null
    unknown_leg_count: number
    possible_overrun_minutes: number | null
    status: 'complete' | 'incomplete' | 'possible_overrun' | string
    buffer_minutes?: number
    budget_minutes?: number
  }
  cost_summary: Summary
  issues: Issue[]
  /** Signed, opaque token for stateless edits; never modify on the client. */
  edit_token: string
}

export interface RouteItinerary {
  schema_version: 3
  planning_conditions: {
    city: string
    start_date: string
    end_date: string
    arrival_at: string
    departure_at: string
    arrival_place_id?: string | null
    departure_place_id?: string | null
    arrival_place?: Place | null
    departure_place?: Place | null
    remarks?: string
    transportation: string
    budget_per_person: number | null
    interpretation_notes?: string[]
    reminder_only_requests?: string[]
  }
  lodging_base: { source: string; user_input: string | null; area_name: string | null; recommendation_reason: string | null; place: Place | null }
  days: RouteDay[]
  cost_summary: Summary
  issues: Issue[]
}

export type AnyRouteItinerary = RouteItinerary | RouteItineraryV2

export interface PoiSearchResult {
  id: string
  name: string
  type?: string
  address: string
  location: { longitude: number; latitude: number }
  tel?: string | null
  photos?: string[]
  opening_hours?: string | null
  reference_cost?: number | null
  cost_basis?: string | null
  [key: string]: unknown
}

export type DayEditOperation =
  | { type: 'add_poi'; poi_id: string; client_activity_id: string }
  | { type: 'delete_activity'; activity_id: string; confirmed: true }
  | { type: 'move_activity'; activity_id: string; direction: 'up' | 'down' }
  | { type: 'select_leg_mode'; from_activity_id: string; to_activity_id: string; mode: TravelMode }

export interface DayEditResponse {
  request_id: string
  client_revision: number
  date: string
  day: RouteDay
}
