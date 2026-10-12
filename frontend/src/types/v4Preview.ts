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
  cost: ReferenceCostSummary
}

export interface V4FinalMetadata {
  save_id: string
  saved_at: string
  source_store_version: number
  plan_revision: number
}

export interface V4FinalState {
  task_id: string
  plan_id: string
  restorable: boolean
  final: V4FinalMetadata | null
  unavailable_reason: 'snapshot_incomplete' | null
}

export interface V4FinalSaveRequest {
  save_id: string
  source_store_version: number
}

export interface V4FinalSaveResponse {
  task_id: string
  plan_id: string
  status: 'saved' | 'already_saved' | 'stale'
  replayed: boolean
  current_store_version: number
  receipt_store_version: number
  restorable: boolean
  final: V4FinalMetadata | null
  unavailable_reason: 'snapshot_incomplete' | null
}

export interface V4FinalRestoreRequest {
  restore_id: string
  expected_store_version: number
  expected_plan_revision: number
  expected_save_id: string
}

export interface V4RestoreResponse {
  envelope: V4PreviewEnvelope
  restored_from: V4FinalMetadata
  replayed: boolean
  receipt_store_version: number
}

export type V4EditOperation =
  | { type: 'add_activity'; client_ref: string; candidate_ref: string; slot_id: string; day_id: string }
  | { type: 'remove_activity'; day_id: string; activity_id: string }
  | { type: 'reorder_activities'; day_id: string; activity_ids: string[] }
  | { type: 'select_leg_mode'; day_id: string; leg_id: string; mode: V4TravelMode; option_id: string }

export interface V4EditCommand {
  plan_id: string
  day_id: string
  base_revision: number
  command_id: string
  operations: V4EditOperation[]
}

export interface V4EditRequest {
  command: V4EditCommand
  expected_store_version: number
}

export interface V4EditJob {
  task_id: string
  plan_id: string
  calculation_id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'superseded' | string
  source_store_version: number
  source_plan_revision: number
  affected_day_ids: string[]
  result_store_version: number | null
  error_code: string | null
  updated_at: string
}

export interface V4EditResponse {
  envelope: V4PreviewEnvelope
  job: V4EditJob | null
  changed_activity_ids: string[]
  changed_leg_ids: string[]
  replayed: boolean
  receipt_store_version: number
}

export interface V4EditJobResponse {
  envelope: V4PreviewEnvelope
  job: V4EditJob
}

export type V4DestinationCategory = 'sightseeing' | 'meal'

export interface V4DestinationSearchRequest {
  keywords: string
  category: V4DestinationCategory
  page: number
}

export interface V4DestinationSearchCandidate {
  candidate_token: string | null
  name: string
  address: string | null
  city: string | null
  location: MapCoordinate | null
  type_name: string | null
  cost: {
    raw: string | null
    value: string | number | null
    unit: 'per_person_reference'
  }
  opening_hours_raw: string | null
  photos: string[]
  missing_fields: Array<{ field: string; reason: string }>
  source: {
    provider: string
    tool_name: string
    api_version: 'v3' | 'v4'
    queried_at: string
    effective_params: Record<string, string | number | boolean | null>
  }
  selectable: boolean
  selection_block_reason: 'location_unavailable' | 'city_mismatch' | 'category_unverified' | 'source_unclosed' | null
}

export interface V4DestinationSearchResponse {
  search_id: string
  city: string
  keywords: string
  category: V4DestinationCategory
  page: number
  status: 'ok' | 'empty' | 'error'
  provider_count: number | null
  candidates: V4DestinationSearchCandidate[]
  omitted_count: number
  error: {
    category: string
    provider_code: string | null
    message: string
    retryable: boolean
  } | null
}

export interface V4DestinationSelectionRequest {
  candidate_token: string
  day_id: string
  slot_id: string
  expected_store_version: number
  expected_plan_revision: number
  command_id: string
  client_ref: string
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
        amount_basis?: 'per_person' | 'whole_party'
        party_basis: string
        party_size: number | null
      }
      arrival_place_query?: string | null
      departure_place_query?: string | null
      lodging_query?: string | null
      lodging_poi_id?: string | null
    }
    requirements?: {
      lodging_changes?: Array<{ start_date: string; city?: string | null; lodging_query?: string | null }>
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
  day_summaries: Array<{
    day_id: string
    known_travel_s: number
    unknown_leg_ids: string[]
    cost_status: 'available' | 'missing' | 'aggregation_pending'
    known_cost_subtotal: string | null
  }>
  calculation: {
    based_on_revision: number
    calculation_id: string
    current_routes: 'pending' | 'completed' | 'failed' | string
    alternative_backfill: 'not_required' | 'pending' | 'running' | 'completed' | 'failed' | string
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
  lodging_day_ids?: string[]
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
