const dayId = 'day-2026-11-05'
const requiredSlots = ['breakfast', 'morning', 'lunch', 'afternoon', 'dinner', 'evening']
const slotId = period => `slot-${dayId}-${period}`
const point = (lng, lat) => ({ lng, lat, coordinate_system: 'GCJ-02' })

function costEntry({ kind, activityId, poiId, name, amount, status = 'known' }) {
  return {
    kind,
    day_id: dayId,
    day_date: '2026-11-05',
    activity_ids: activityId ? [activityId] : [],
    slot_ids: [],
    poi_id: poiId,
    place_name: name,
    amount,
    status,
    unknown_reason: status === 'unknown' ? 'cost_not_returned' : null,
    evidence: [{
      place_fact_id: `fact-${poiId}`,
      cost_source: { provider: 'amap', queried_at: '2026-11-01T04:20:00Z' },
      sources: [{ provider: 'amap', queried_at: '2026-11-01T04:20:00Z' }],
    }],
  }
}

export function mixedSyntheticEnvelope() {
  const activities = [
    { activity_id: 'activity-sight-1', candidate_ref: 'candidate-selected-sight', slot_id: slotId('morning'), kind: 'sightseeing', poi_id: 'poi-sight-1', place_fact_id: 'fact-poi-sight-1', requirement_ids: [], recommendation_reason: '步行游览街区。' },
    { activity_id: 'activity-meal-1', candidate_ref: null, slot_id: slotId('lunch'), kind: 'meal', poi_id: 'poi-meal-1', place_fact_id: 'fact-poi-meal-1', requirement_ids: [], recommendation_reason: '午餐安排。' },
    { activity_id: 'activity-sight-2', candidate_ref: null, slot_id: slotId('afternoon'), kind: 'sightseeing', poi_id: 'poi-sight-2', place_fact_id: 'fact-poi-sight-2', requirement_ids: [], recommendation_reason: '下午游览。' },
  ]
  const places = [
    { fact_id: 'fact-poi-sight-1', poi_id: 'poi-sight-1', name: '外滩', location: point(121.49, 31.24), address: '中山东一路', business_area: '外滩', source_refs: ['source-1'], field_sources: { cost: 'source-1' }, cost: { value: '50', unit: 'per_person_reference' } },
    { fact_id: 'fact-poi-meal-1', poi_id: 'poi-meal-1', name: '午市小馆', location: point(121.495, 31.238), address: '南京东路', business_area: '南京东路', source_refs: ['source-1'], cost: { value: null, unit: 'per_person_reference' } },
    { fact_id: 'fact-poi-sight-2', poi_id: 'poi-sight-2', name: '豫园', location: point(121.49, 31.227), address: '安仁街', business_area: '豫园', source_refs: ['source-1'], cost: { value: '40', unit: 'per_person_reference' } },
    { fact_id: 'fact-hotel', poi_id: 'poi-hotel', name: '滨江酒店', location: point(121.50, 31.235), address: '中山东一路附近', business_area: '外滩', source_refs: ['source-1'], cost: { value: '360', unit: 'per_person_reference' } },
    ...['candidate-sight-1', 'candidate-sight-2', 'candidate-sight-3', 'candidate-meal-1', 'candidate-meal-2'].map((id, index) => ({
      fact_id: `fact-${id}`,
      poi_id: `poi-${id}`,
      name: `${id.includes('meal') ? '候选餐馆' : '候选景点'}${index + 1}`,
      location: point(121.48 + index * 0.002, 31.23 + index * 0.002),
      address: `测试路${index + 1}号`,
      business_area: '测试区域',
      source_refs: ['source-1'],
      cost: { value: null, unit: 'per_person_reference' },
    })),
  ]
  const candidates = [
    { candidate_id: 'candidate-selected-sight', category: 'sightseeing', poi_id: 'poi-sight-1', place_fact_id: 'fact-poi-sight-1', area_hint_id: null, slot_ids: [slotId('morning')], requirement_ids: [], recommendation_reason: '已加入行程。' },
    ...['candidate-sight-1', 'candidate-sight-2', 'candidate-sight-3'].map((id, index) => ({ candidate_id: id, category: 'sightseeing', poi_id: `poi-${id}`, place_fact_id: `fact-${id}`, area_hint_id: null, slot_ids: [slotId('afternoon')], requirement_ids: [], recommendation_reason: `可选景点 ${index + 1}` })),
    ...['candidate-meal-1', 'candidate-meal-2'].map((id, index) => ({ candidate_id: id, category: 'meal', poi_id: `poi-${id}`, place_fact_id: `fact-${id}`, area_hint_id: null, slot_ids: [slotId('dinner')], requirement_ids: [], recommendation_reason: `可选餐馆 ${index + 1}` })),
    { candidate_id: 'candidate-hotel', category: 'lodging', poi_id: 'poi-hotel', place_fact_id: 'fact-hotel', area_hint_id: 'area-bund', slot_ids: [], requirement_ids: [], recommendation_reason: '靠近当天行程。' },
  ]
  const routeOptions = [
    { option_id: 'transit-long', duration_s: 1800, geometry_status: 'complete', segments: [{ sequence: 0, mode: 'walking', polyline: [point(121.49, 31.24), point(121.495, 31.238)], transit_alternatives: [] }] },
    { option_id: 'transit-short', duration_s: 1200, geometry_status: 'complete', segments: [{ sequence: 0, mode: 'walking', polyline: [point(121.49, 31.24), point(121.495, 31.238)], transit_alternatives: [] }] },
  ]
  const routeLeg = {
    leg_id: 'leg-sight-meal',
    day_id: dayId,
    from_endpoint: { kind: 'activity', reference_id: 'activity-sight-1', poi_id: 'poi-sight-1' },
    to_endpoint: { kind: 'activity', reference_id: 'activity-meal-1', poi_id: 'poi-meal-1' },
    requested_departure_at: '2026-11-05T09:00:00+08:00',
    time_basis: 'reference',
    mode_results: [
      { mode: 'transit', status: 'available', route_fact_id: 'route-transit', option_ids: ['transit-long', 'transit-short'] },
      { mode: 'bicycling', status: 'available', route_fact_id: 'route-bike', option_ids: ['bike-no-duration', 'bike-long', 'bike-short'] },
      { mode: 'walking', status: 'available', route_fact_id: 'route-walk', option_ids: ['walk-long'] },
    ],
    selected_mode: 'transit',
    selected_option_id: 'transit-long',
    selection_source: 'backend_fastest_available',
  }
  const returnLeg = {
    leg_id: 'leg-return',
    day_id: dayId,
    from_endpoint: { kind: 'activity', reference_id: 'activity-sight-2', poi_id: 'poi-sight-2' },
    to_endpoint: { kind: 'departure', reference_id: 'departure', poi_id: 'poi-airport' },
    requested_departure_at: '2026-11-05T18:00:00+08:00',
    time_basis: 'computed',
    mode_results: [
      { mode: 'transit', status: 'available', route_fact_id: 'route-return-transit', option_ids: ['return-transit'] },
      { mode: 'bicycling', status: 'available', route_fact_id: 'route-return-bike', option_ids: ['return-bike'] },
      { mode: 'walking', status: 'empty', route_fact_id: null, option_ids: [] },
    ],
    selected_mode: 'transit',
    selected_option_id: 'return-transit',
    selection_source: 'backend_fastest_available',
  }
  const simpleRoute = (factId, mode, options) => ({ fact_id: factId, origin_poi_id: 'poi-sight-1', destination_poi_id: 'poi-meal-1', mode, status: 'available', options })
  const issues = [
    { issue_id: 'issue-missing-evening', code: 'missing_sightseeing', category: 'missing_arrangement', severity: 'error', scope: 'slot', day_id: dayId, target_id: slotId('evening'), message: '晚上尚未安排景点。' },
    { issue_id: 'issue-conditional-breakfast', code: 'conditional_slot_unresolved', category: 'unknown_info', severity: 'warning', scope: 'slot', day_id: dayId, target_id: slotId('breakfast'), message: '早餐是否需要安排仍待确认。' },
    { issue_id: 'issue-activity-conflict', code: 'opening_hours_conflict', category: 'known_conflict', severity: 'warning', scope: 'activity', day_id: dayId, target_id: 'activity-sight-1', message: '营业时间可能与游览安排冲突。' },
    { issue_id: 'issue-slot-conflict', code: 'slot_conflict', category: 'known_conflict', severity: 'warning', scope: 'slot', day_id: dayId, target_id: slotId('afternoon'), message: '下午时段存在安排冲突。' },
    { issue_id: 'issue-leg-unknown', code: 'route_unverified', category: 'unknown_info', severity: 'warning', scope: 'leg', day_id: dayId, target_id: 'leg-sight-meal', message: '该路段仍需核实。' },
    { issue_id: 'issue-return-conflict', code: 'return_route_unverified', category: 'known_conflict', severity: 'warning', scope: 'leg', day_id: dayId, target_id: 'leg-return', message: '返程路段与离开时间存在冲突。' },
    { issue_id: 'issue-unbound-activity', code: 'activity_reference_unbound', category: 'unknown_info', severity: 'warning', scope: 'activity', day_id: dayId, target_id: 'activity-missing', message: '该活动问题无法关联到当前卡片。' },
    { issue_id: 'issue-must-visit', code: 'must_visit_unplanned', category: 'missing_arrangement', severity: 'error', scope: 'plan', day_id: null, target_id: 'must-visit-1', message: '必去要求“外滩夜景”尚未核实。' },
  ]
  const daySlots = requiredSlots.map(period => ({ slot_id: slotId(period), day_id: dayId, period, eligibility: period === 'breakfast' ? 'conditional' : 'required', condition_reason: period === 'breakfast' ? '实际抵达时刻未核实' : null }))
  const entries = [
    costEntry({ kind: 'sightseeing', activityId: 'activity-sight-1', poiId: 'poi-sight-1', name: '外滩', amount: '50' }),
    costEntry({ kind: 'meal', activityId: 'activity-meal-1', poiId: 'poi-meal-1', name: '午市小馆', amount: null, status: 'unknown' }),
    costEntry({ kind: 'sightseeing', activityId: 'activity-sight-2', poiId: 'poi-sight-2', name: '豫园', amount: '40' }),
  ]
  const envelope = {
    task_id: 'synthetic-mixed-task',
    plan_id: 'synthetic-mixed-plan',
    store_version: 1,
    plan_revision: 1,
    preview: {
      schema_version: 4,
      preview_id: 'synthetic-preview-mixed',
      planning_input: {
        request: { request_id: 'synthetic-request', city: '上海', arrival_at: '2026-11-05T08:00:00+08:00', departure_at: '2026-11-07T19:00:00+08:00', timezone: 'Asia/Shanghai', budget: { amount: '1200', currency: 'CNY', scope: 'whole_trip', party_basis: 'per_person', party_size: 1 }, arrival_place_query: null, departure_place_query: null, lodging_query: null },
        days: [{ day_id: dayId, day_date: '2026-11-05', slots: daySlots }],
      },
      draft: {
        plan_id: 'synthetic-mixed-plan', revision: 1,
        days: [{ day_id: dayId, day_date: '2026-11-05', theme: '沿着黄浦江漫步', activities, unfilled_slot_ids: [slotId('evening')], weather_fact_id: 'weather-1', weather_advice: '白天有阵雨，建议携带雨具。', lodging: { candidate_ref: 'candidate-hotel', poi_id: 'poi-hotel', place_fact_id: 'fact-hotel', area_hint_id: 'area-bund', selection_source: 'recommended' } }],
        area_hints: [{ area_hint_id: 'area-bund', label: '外滩与南京东路一带', center: point(121.495, 31.237) }],
        legs: [routeLeg, returnLeg],
      },
      facts: {
        sources: [{ tool_call_id: 'source-1', provider: 'amap', tool_name: 'maps_search', queried_at: '2026-11-01T04:20:00Z' }],
        places,
        weather: [{ fact_id: 'weather-1', forecasts: [{ forecast_date: '2026-11-05', day_weather: '小雨', night_weather: '多云', day_temp_c: 18, night_temp_c: 14 }] }],
        routes: [
          simpleRoute('route-transit', 'transit', routeOptions),
          simpleRoute('route-bike', 'bicycling', [{ option_id: 'bike-no-duration', duration_s: null, geometry_status: 'missing', segments: [] }, { option_id: 'bike-long', duration_s: 900, geometry_status: 'missing', segments: [] }, { option_id: 'bike-short', duration_s: 420, geometry_status: 'missing', segments: [] }]),
          simpleRoute('route-walk', 'walking', [{ option_id: 'walk-long', duration_s: 2400, geometry_status: 'missing', segments: [] }]),
          simpleRoute('route-return-transit', 'transit', [{ option_id: 'return-transit', duration_s: 3600, geometry_status: 'missing', segments: [] }]),
          simpleRoute('route-return-bike', 'bicycling', [{ option_id: 'return-bike', duration_s: 1800, geometry_status: 'missing', segments: [] }]),
        ],
      },
      candidates,
      validation: { issues, days: [{ day_id: dayId, completeness: 'incomplete', feasibility: 'unverified', issue_ids: issues.filter(issue => issue.day_id === dayId).map(issue => issue.issue_id) }] },
      day_summaries: [{ day_id: dayId, known_travel_s: 1800, unknown_leg_ids: [], cost_status: "available", known_cost_subtotal: "90" }],
      calculation: { based_on_revision: 1, calculation_id: 'synthetic-calc', current_routes: 'completed', alternative_backfill: 'completed', pending_leg_ids: [] },
    },
    cost: { request_id: 'synthetic-request', plan_id: 'synthetic-mixed-plan', based_on_revision: 1, status: 'known_partial', known_sightseeing_meal_per_person_subtotal: '90', days: [{ day_id: dayId, day_date: '2026-11-05', status: 'known_partial', known_sightseeing_meal_per_person_subtotal: '90', entries, lodging: costEntry({ kind: 'lodging', poiId: 'poi-hotel', name: '滨江酒店', amount: '360' }) }], unit: 'per_person_reference', scope: 'sightseeing_and_meals_only' },
  }
  return envelope
}

export function shortDepartureSyntheticEnvelope() {
  const envelope = mixedSyntheticEnvelope()
  const day = envelope.preview.draft.days[0]
  const dayValidation = envelope.preview.validation.days[0]
  const required = envelope.preview.planning_input.days[0].slots.find(slot => slot.period === 'evening')
  required.eligibility = 'required'
  day.unfilled_slot_ids = [required.slot_id]
  dayValidation.completeness = 'not_required'
  return envelope
}
