import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { mixedSyntheticEnvelope, shortDepartureSyntheticEnvelope } from './v4PreviewFixtures.mjs'

const source = await readFile(new URL('../src/utils/v4PreviewMapper.ts', import.meta.url), 'utf8')
const pageSource = await readFile(new URL('../src/views/TripPreview.vue', import.meta.url), 'utf8')
const exportSource = await readFile(new URL('../src/views/TripPreviewExport.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { formatPreviewEvidenceTime, formatReferenceDuration, mapV4Preview } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const mixed = mapV4Preview(mixedSyntheticEnvelope())
const day = mixed.days[0]
assert.equal(mixed.cost.knownSubtotal, '90', 'display the backend subtotal without recalculating it')
assert.equal(day.cost.unknownCount, 1)
assert.equal(day.activities[1].cost.amount, null, 'unknown cost must remain unknown, not zero')
assert.match(day.activities[1].cost.text, /未查询到/)
assert.match(day.activities[0].cost.evidenceLabel, /高德地图.*查询于/)
assert.equal(mixed.planIssues.length, 1, 'global must-visit issue stays visible')
assert.ok(!day.issues.some(issue => issue.issue_id === 'issue-activity-conflict'), 'opening-time conclusions are hidden in all notice areas')
assert.ok(day.legs[0].issues.some(issue => issue.issue_id === 'issue-leg-unknown'), 'route issues stay beside their leg')
assert.ok(day.unboundIssues.some(issue => issue.issue_id === 'issue-unbound-activity'), 'unbound issues fall back to the day notice area')
assert.deepEqual(day.missingSlots.map(slot => [slot.period, slot.kind]), [['breakfast', 'conditional'], ['evening', 'missing']])
assert.match(day.missingSlots[0].prompt, /待确认/)
assert.match(day.missingSlots[1].prompt, /尚未安排景点/)
assert.ok(!('returnTiming' in day), 'the shared preview/export view has no return feasibility result')
assert.equal(day.returnLeg.id, 'leg-return', 'departure route stays available as a separate return leg')
assert.equal(day.returnLeg.modes.length, 3, 'return route displays unified public transport, cycling, and walking')
assert.ok(!day.returnLeg.issues.some(issue => issue.issue_id === 'issue-return-conflict'), 'return transport stays visible without feasibility conclusions')
assert.ok(day.activities.every(activity => !('timingLabel' in activity) && !('durationLabel' in activity) && !('timing' in activity)))
assert.deepEqual(Object.keys(day.transport).sort(), ['knownTravelSeconds', 'unknownLegCount'])
assert.equal(day.transport.knownTravelSeconds, 1800, 'transport durations are retained')
assert.equal(day.area.label, '外滩与南京东路一带', 'unspecified lodging retains its recommendation area')
assert.ok(day.locations.some(location => location.kind === 'area'))
assert.equal(day.legs[0].modes.find(mode => mode.mode === 'bicycling').durationSeconds, 420, 'non-selected modes use their shortest valid option')
assert.equal(day.legs[0].paths.length, 1, 'draw only geometry from the selected route option')
assert.match(day.legs[0].timeBasisLabel, /公共交通按.*09:00 参考时刻查询/)

const walkingEnvelope = mixedSyntheticEnvelope()
walkingEnvelope.preview.calculation.alternative_backfill = 'not_required'
const walkingLeg = walkingEnvelope.preview.draft.legs[0]
walkingLeg.selected_mode = 'walking'
walkingLeg.selected_option_id = 'walk-long'
const walkingView = mapV4Preview(walkingEnvelope).days[0].legs[0]
assert.equal(walkingView.selectedDurationLabel, '0小时40分钟')
assert.equal(walkingView.paths.length, 0, 'missing walking geometry cannot borrow a transit path')
assert.match(walkingView.timeBasisLabel, /公共交通按.*09:00 参考时刻查询/, 'the transit reference remains explicit when walking is selected')
assert.equal(day.recommendations.filter(candidate => candidate.kind === 'sightseeing').length, 2)
assert.equal(day.recommendations.filter(candidate => candidate.kind === 'meal').length, 1)
assert.ok(!day.recommendations.some(candidate => candidate.id === 'candidate-selected-sight'))
assert.equal(formatReferenceDuration(1800), '0小时30分钟')

const recoveryEnvelope = mixedSyntheticEnvelope()
const keptActivity = recoveryEnvelope.preview.draft.days[0].activities[0]
recoveryEnvelope.preview.validation.issues.push({
  issue_id: 'initial-omitted-morning', code: 'INITIAL_ACTIVITY_OMITTED',
  category: 'unknown_info', severity: 'warning', scope: 'slot',
  day_id: recoveryEnvelope.preview.draft.days[0].day_id, target_id: keptActivity.slot_id,
  message: '该时段有初版活动未能通过校验，未纳入行程；其他有效安排已保留，请检查当前安排。',
  triggers_repair: false, based_on_revision: recoveryEnvelope.preview.draft.revision,
})
const recoveredDay = mapV4Preview(recoveryEnvelope).days[0]
assert.deepEqual(recoveredDay.activities.map(a => a.id), day.activities.map(a => a.id), 'a recovery notice cannot remove valid activities')
assert.ok(recoveredDay.activities[0].issues.some(issue => issue.issue_id === 'initial-omitted-morning'), 'a dropped activity stays visible beside the kept activity in the same slot')
assert.deepEqual(recoveredDay.missingSlots.map(slot => slot.period), day.missingSlots.map(slot => slot.period), 'historical recovery notice must not invent a new current missing slot')

const readableEnvelope = mixedSyntheticEnvelope()
const rawRouteIssue = readableEnvelope.preview.validation.issues.find(issue => issue.issue_id === 'issue-leg-unknown')
rawRouteIssue.message = '路段耗时尚未核实：missing_route_leg。'
const dayId = readableEnvelope.preview.draft.days[0].day_id
readableEnvelope.preview.validation.issues.push(
  {
    issue_id: 'issue-short-day', code: 'departure_day_below_minimum_with_activities',
    category: 'known_conflict', severity: 'error', scope: 'day', day_id: dayId, target_id: null,
    message: 'F07 已证实离开日可用时间不足3小时；现有活动仍保留，但需要调整。',
  },
  {
    issue_id: 'issue-activity-overrun', code: 'activity_exceeds_available_end',
    category: 'known_conflict', severity: 'error', scope: 'activity', day_id: dayId,
    target_id: 'activity-sight-1', message: 'F07已证实活动结束时间超过该日已核实的可用终点。',
  },
)
const rawRouteMessage = rawRouteIssue.message
const readable = mapV4Preview(readableEnvelope)
const readableDay = readable.days[0]
assert.equal(readableDay.legs[0].issues.find(issue => issue.issue_id === 'issue-leg-unknown').message,
  '尚未查到该路段耗时。')
assert.ok(!readableDay.legs[0].issues.some(issue => /missing_route_leg|F07/.test(issue.message)))
assert.ok(!readableDay.issues.some(issue => issue.issue_id === 'issue-short-day' || issue.issue_id === 'issue-activity-overrun'))
assert.ok(!readableDay.issues.some(issue => /\bF07\b|missing_route_leg/.test(issue.message)))
assert.equal(rawRouteIssue.message, rawRouteMessage, 'mapping must not rewrite the original envelope')
assert.match(formatPreviewEvidenceTime('2026-11-01T04:20:00Z'), /\d{1,2}(月|\/)\d{1,2}/)
assert.doesNotMatch(formatPreviewEvidenceTime('2026-11-01T04:20:00Z'), /T|Z/)
assert.equal(formatPreviewEvidenceTime('not-a-time'), null)
assert.match(exportSource, /formatPreviewEvidenceTime\(source\.queried_at\)/)
assert.doesNotMatch(exportSource, /\{\{\s*source\.queried_at\s*\}\}/, 'export must not render raw machine timestamps')

const numberedEnvelope = mixedSyntheticEnvelope()
const numberedDraftDay = numberedEnvelope.preview.draft.days[0]
const [firstPlace, , secondPlace] = numberedDraftDay.activities
const freeTime = {
  activity_id: 'activity-free-time',
  candidate_ref: null,
  slot_id: 'slot-day-2026-11-05-lunch',
  kind: 'free_time',
  poi_id: null,
  place_fact_id: null,
  requirement_ids: [],
  recommendation_reason: '附近自由活动。',
}
numberedDraftDay.activities = [firstPlace, freeTime, secondPlace]
const numberedDay = mapV4Preview(numberedEnvelope).days[0]
const destinationNumbers = numberedDay.locations
  .filter(location => location.kind === 'activity')
  .map(location => location.number)
const activityPositions = numberedDay.timeline
  .filter(item => item.type === 'activity')
  .map(item => item.position)
assert.deepEqual(destinationNumbers, [1, 3], 'map pins retain their original activity positions across a free-time slot')
assert.deepEqual(activityPositions, [1, 2, 3])

const samePlaceEnvelope = mixedSyntheticEnvelope()
const samePlaceDay = samePlaceEnvelope.preview.draft.days[0]
samePlaceDay.activities[2] = {
  ...samePlaceDay.activities[2],
  poi_id: samePlaceDay.activities[0].poi_id,
  place_fact_id: samePlaceDay.activities[0].place_fact_id,
}
const samePlaceLeg = samePlaceEnvelope.preview.draft.legs[0]
samePlaceLeg.to_endpoint = {
  kind: 'activity',
  reference_id: samePlaceDay.activities[2].activity_id,
  poi_id: samePlaceDay.activities[0].poi_id,
}
samePlaceLeg.requested_departure_at = null
samePlaceLeg.time_basis = 'unspecified'
samePlaceLeg.mode_results = []
samePlaceLeg.selected_mode = null
samePlaceLeg.selected_option_id = null
samePlaceLeg.selection_source = 'same_place'
const samePlaceView = mapV4Preview(samePlaceEnvelope).days[0].legs.find(leg => leg.id === samePlaceLeg.leg_id)
assert.equal(samePlaceView.selectedModeLabel, '同一地点，无需交通')
assert.equal(samePlaceView.selectedDurationLabel, '0小时0分钟')
assert.equal(samePlaceView.modes.length, 0, 'same-place activity pairs do not show three missing route modes')
assert.equal(samePlaceView.paths.length, 0, 'same-place activity pairs do not draw a route')
assert.equal(samePlaceView.geometryStatus, 'not_required', 'same-place movement is not an unselected route')
assert.equal(samePlaceView.timeBasisLabel, null)

assert.match(pageSource, /全程旅费预算 ¥\{\{ view\.tripBudget \}\}/)
assert.doesNotMatch(pageSource, /全程旅费预算 ¥\{\{ view\.tripBudget \}\} \/ 人/, 'whole-trip budget has no per-person label')
assert.match(pageSource, /已知人均景点与餐饮消费参考/)

const short = mapV4Preview(shortDepartureSyntheticEnvelope())
assert.equal(short.days[0].missingSlots.length, 0, 'F07 short departure days remain exempt even when input slots are required')
assert.ok(!('returnTiming' in short.days[0]))

const transitionsEnvelope = mixedSyntheticEnvelope()
transitionsEnvelope.preview.day_summaries[0].unknown_leg_ids = ['leg-known', 'leg-summary-only']
assert.equal(mapV4Preview(transitionsEnvelope).days[0].transport.unknownLegCount, 2, 'unknown transport count comes from route summary without timing')
assert.ok(!('timing' in transitionsEnvelope), 'new snapshots contain no activity time snapshot')

console.log('v4PreviewMapper: mixed synthetic and short-departure assertions passed')

for (const lodgingInput of [{ lodging_poi_id: 'poi-hotel' }, { lodging_query: '用户已订酒店' }, {}]) {
  const specifiedEnvelope = mixedSyntheticEnvelope()
  Object.assign(specifiedEnvelope.preview.planning_input.request, lodgingInput)
  if (!Object.keys(lodgingInput).length) specifiedEnvelope.preview.draft.days[0].lodging.selection_source = 'user'
  const original = JSON.stringify(specifiedEnvelope)
  const specifiedDay = mapV4Preview(specifiedEnvelope).days[0]
  assert.equal(specifiedDay.area, null, 'selected POI, typed lodging, and user-selected lodging all hide the recommendation area')
  assert.ok(!specifiedDay.locations.some(location => location.kind === 'area'), 'hidden areas cannot leak into map pins or legend')
  assert.ok(specifiedDay.locations.some(location => location.kind === 'hotel'), 'the real hotel map marker is retained')
  assert.equal(specifiedDay.hotel.name, day.hotel.name)
  assert.equal(specifiedDay.hotel.label, '已指定住处')
  assert.equal(JSON.stringify(specifiedEnvelope), original, 'display mapping never modifies the API snapshot')
}

const hiddenEnvelope = mixedSyntheticEnvelope()
for (const [index, code] of ['timing_not_provided', 'activity_timing_unverified', 'activity_exceeds_available_end',
  'return_deadline_conflict', 'return_timing_unverified', 'return_route_unverified',
  'opening_hours_unavailable', 'opening_hours_unverified'].entries()) {
  hiddenEnvelope.preview.validation.issues.push({
    issue_id: `hidden-${index}`, code, category: 'known_conflict', severity: 'warning',
    scope: index % 2 ? 'activity' : 'plan', day_id: index % 2 ? day.id : null,
    target_id: index % 2 ? day.activities[0].id : null, message: '活动时间或返程结论不应展示。',
  })
}
const hiddenView = mapV4Preview(hiddenEnvelope)
assert.doesNotMatch(JSON.stringify(hiddenView), /hidden-\d|活动时间或返程结论不应展示/)
assert.ok(hiddenView.planIssues.some(issue => issue.code === 'must_visit_unplanned'), 'real missing destination notices remain')
for (const template of [pageSource, exportSource]) {
  assert.doesNotMatch(template, /activity\.(durationLabel|timingLabel)|returnTiming|knownStaySeconds|elapsedSeconds|openingHoursLabel/)
}
console.log('v4PreviewMapper: S01 display, specified-lodging map, and source-preservation checks passed')

const changedLodging = mixedSyntheticEnvelope()
changedLodging.preview.planning_input.request.lodging_query = '前几天指定住处'
changedLodging.preview.planning_input.requirements = {
  lodging_changes: [{ start_date: changedLodging.preview.draft.days[0].day_date, city: '苏州', lodging_query: null }],
}
const changedLodgingView = mapV4Preview(changedLodging).days[0]
assert.equal(changedLodgingView.hotel.label, '推荐酒店', 'explicit change without new residence permits recommendation on changed dates')
assert.equal(changedLodgingView.area.label, day.area.label)
changedLodging.preview.planning_input.requirements.lodging_changes[0].lodging_query = '新的指定住处'
changedLodging.preview.draft.days[0].lodging.selection_source = 'user'
const newSpecifiedLodgingView = mapV4Preview(changedLodging).days[0]
assert.equal(newSpecifiedLodgingView.hotel.label, '已指定住处')
assert.equal(newSpecifiedLodgingView.area, null, 'newly specified residence hides accommodation recommendations')
