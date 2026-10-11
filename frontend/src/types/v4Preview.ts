/** v4 预览只读页面消费的 API 契约。字段与 backend/app/models/planning_v4 对齐。 */

export type V4TravelMode = 'transit' | 'bicycling' | 'walking'
export type V4SlotPeriod = 'breakfast' | 'morning' | 'lunch' | 'afternoon' | 'dinner' | 'evening'

export interface V4PreviewAccess {
  taskId: string
  planId: string
  accessToken: string
}

export interface V4TaskStatus {
  task_id: string
  plan_id: string
  status: 'accepted' | 'running' | 'ready' | 'failed' | 'interrupted' | string
  stage: string
  completed: number
  total: number
  message: string | null
  backfill: {
    status: 'not_started' | 'running' | 'completed' | 'failed' | 'interrupted' | string
    completed: number
    total: number
  }
  preview_ready: boolean
  store_version: number | null
  error_code: string | null
  updated_at: string
}

export interface V4PreviewEnvelope {
  task_id: string
  plan_id: string
  store_version: number
  plan_revision: number
  preview: ItineraryPreview
  timing: PlanTimingResult
  cost: ReferenceCostSummary
}

export interface ItineraryPreview {
  schema_version: 4
  preview_id: string
  planning_input: {
    request: {
      request_id: string
      city: string
      arrival_at: string
      departure_at: string
      timezone: string
      budget?: {
        amount: string | number | null
        currency: string
        scope: 'whole_trip'
        party_basis: string
        party_size: number | null
      }
      arrival_place_query?: string | null
      departure_place_query?: string | null
      lodging_query?: string | null
    }
    days: DayConstraint[]
  }
  draft: {
    plan_id: string
    revision: number
    days: DraftDay[]
    area_hints: AreaHint[]
    legs: RouteLeg[]
  }
  facts: {
    sources?: FactSource[]
    places: PlaceFact[]
    weather: WeatherFact[]
    routes: RouteFact[]
  }
  candidates: CandidateRef[]
  validation: {
    issues: ValidationIssue[]
    days: ValidationDay[]
  }
  calculation: {
    based_on_revision: number
    calculation_id: string
    current_routes: 'pending' | 'completed' | 'failed' | string
    alternative_backfill: 'pending' | 'running' | 'completed' | 'failed' | string
    pending_leg_ids: string[]
  }
}

export interface DayConstraint {
  day_id: string
  day_date: string
  slots: SlotConstraint[]
}

export interface SlotConstraint {
  slot_id: string
  day_id: string
  period: V4SlotPeriod
  eligibility: 'required' | 'skipped' | 'conditional'
  condition_reason?: string | null
}

export interface DraftDay {
  day_id: string
  day_date: string
  theme: string
  activities: DraftActivity[]
  unfilled_slot_ids: string[]
  weather_fact_id: string | null
  weather_advice: string | null
  lodging: LodgingRef | null
}

export interface DraftActivity {
  activity_id: string
  candidate_ref: string | null
  slot_id: string
  kind: 'sightseeing' | 'meal' | 'free_time'
  poi_id: string | null
  place_fact_id: string | null
  duration_s: number
  duration_source: 'model' | 'default' | 'user'
  duration_reason?: string | null
  requirement_ids: string[]
  recommendation_reason: string
}

export interface LodgingRef {
  candidate_ref: string | null
  poi_id: string | null
  place_fact_id: string | null
  area_hint_id: string | null
  selection_source: 'user' | 'recommended' | 'unknown'
}

export interface AreaHint {
  area_hint_id: string
  label: string
  center?: MapCoordinate | null
}

export interface CandidateRef {
  candidate_id: string
  category: 'sightseeing' | 'meal' | 'lodging'
  poi_id: string
  place_fact_id: string
  area_hint_id?: string | null
  slot_ids: string[]
  requirement_ids: string[]
  recommendation_reason: string
}

export interface PlaceFact {
  fact_id: string
  poi_id: string
  name: string
  location?: MapCoordinate | null
  address?: string | null
  business_area?: string | null
  source_refs?: string[]
  field_sources?: Record<string, string>
  cost: {
    raw?: string | null
    value?: string | number | null
    unit: 'per_person_reference' | string
  }
}

export interface FactSource {
  tool_call_id: string
  provider: 'amap' | string
  tool_name: string
  queried_at: string
}

export interface MapCoordinate {
  lng: number
  lat: number
  coordinate_system?: string
}

export interface WeatherFact {
  fact_id: string
  forecasts: WeatherForecast[]
}

export interface WeatherForecast {
  forecast_date: string
  day_weather?: string | null
  night_weather?: string | null
  day_temp_c?: number | null
  night_temp_c?: number | null
  day_wind?: string | null
  night_wind?: string | null
  day_power?: string | null
  night_power?: string | null
}

export interface ValidationIssue {
  issue_id: string
  code: string
  category: 'missing_arrangement' | 'known_conflict' | 'unknown_info' | 'invalid_reference' | string
  severity: 'info' | 'warning' | 'error' | string
  scope: 'plan' | 'day' | 'slot' | 'activity' | 'leg' | string
  day_id: string | null
  target_id: string | null
  message: string
}

export interface ValidationDay {
  day_id: string
  completeness: 'complete' | 'incomplete' | 'not_required' | 'conditional' | string
  feasibility: 'verified' | 'conflict' | 'unverified' | string
  issue_ids: string[]
}

export interface RouteLeg {
  leg_id: string
  day_id: string
  from_endpoint: LegEndpoint
  to_endpoint: LegEndpoint
  requested_departure_at: string | null
  time_basis: 'computed' | 'reference' | 'unspecified'
  mode_results: RouteModeResult[]
  selected_mode: V4TravelMode | null
  selected_option_id: string | null
  selection_source: 'backend_fastest_available' | 'user' | 'same_place' | null
}

export interface LegEndpoint {
  kind: 'activity' | 'lodging' | 'arrival' | 'departure'
  reference_id: string
  poi_id: string
}

export interface RouteModeResult {
  mode: V4TravelMode
  status: 'pending' | 'available' | 'empty' | 'error' | string
  route_fact_id: string | null
  option_ids: string[]
}

export interface RouteFact {
  fact_id: string
  origin_poi_id: string
  destination_poi_id: string
  mode: V4TravelMode
  status: 'available' | 'empty' | 'error' | string
  options: RouteOption[]
}

export interface RouteOption {
  option_id: string
  duration_s: number | null
  distance_m?: number | null
  geometry_status: 'complete' | 'partial' | 'missing'
  segments: RouteSegment[]
}

export interface RouteSegment {
  sequence: number
  mode: 'walking' | 'bicycling' | 'bus' | 'subway' | 'railway'
  polyline: MapCoordinate[]
  transit_alternatives: Array<{
    mode: 'bus' | 'subway' | 'railway'
    polyline: MapCoordinate[]
  }>
}

export interface PlanTimingResult {
  plan_id: string
  based_on_revision: number
  days: DayTimingResult[]
}

export interface DayTimingResult {
  day_id: string
  activities: ActivityTiming[]
  summary: {
    day_id: string
    known_stay_s: number
    known_travel_s: number
    unknown_leg_ids: string[]
    feasibility: 'verified' | 'conflict' | 'unverified'
  }
  scheduling_decision: 'scheduled' | 'conditional' | 'not_scheduled_insufficient_time'
  elapsed_s: number | null
  unknown_transitions: Array<{ leg_id: string | null; reason: string }>
  return_timing: ReturnTiming
}

export interface ReturnTiming {
  status: 'not_applicable' | 'verified' | 'conflict' | 'unverified' | string
  departure_poi_id: string | null
  departure_at: string | null
  deadline_at: string | null
  latest_activity_end_at: string | null
  estimated_arrival_at: string | null
  earliest_possible_arrival_at: string | null
  route_leg_id: string | null
  travel_s: number | null
  buffer_s: number
  usable_s: number | null
  optimistic_usable_s: number | null
  overrun_s: number | null
  overrun_is_lower_bound: boolean
  unknown_reasons: string[]
  notes: string[]
}

export interface ActivityTiming {
  activity_id: string
  start_at: string | null
  end_at: string | null
  duration_s: number
  status: 'estimated' | 'unverified'
  unknown_reason: string | null
  note: string | null
}

export interface ReferenceCostSummary {
  request_id: string
  plan_id: string
  based_on_revision: number
  status: 'no_activities' | 'all_unknown' | 'known_partial' | 'all_known'
  known_sightseeing_meal_per_person_subtotal: string | number
  days: DayReferenceCostSummary[]
  unit: 'per_person_reference'
  scope: 'sightseeing_and_meals_only'
}

export interface DayReferenceCostSummary {
  day_id: string
  day_date: string
  status: 'no_activities' | 'all_unknown' | 'known_partial' | 'all_known'
  known_sightseeing_meal_per_person_subtotal: string | number
  entries: ReferenceCostEntry[]
  lodging: ReferenceCostEntry | null
}

export interface ReferenceCostEntry {
  kind: 'sightseeing' | 'meal' | 'lodging'
  day_id: string
  day_date: string
  activity_ids: string[]
  slot_ids: string[]
  poi_id: string
  place_name: string
  amount: string | number | null
  status: 'known' | 'unknown' | 'conflict'
  unknown_reason: 'cost_not_returned' | 'conflicting_values' | null
  evidence: Array<{
    place_fact_id: string
    cost_source?: CostSource | null
    sources: CostSource[]
  }>
}

export interface CostSource {
  provider: 'amap' | string
  queried_at: string
}
