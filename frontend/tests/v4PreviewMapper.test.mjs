import assert from 'node:assert/strict'
import { access, readFile } from 'node:fs/promises'
import ts from 'typescript'
import { mixedSyntheticEnvelope, shortDepartureSyntheticEnvelope } from './v4PreviewFixtures.mjs'

const source = await readFile(new URL('../src/utils/v4PreviewMapper.ts', import.meta.url), 'utf8')
const pageSource = await readFile(new URL('../src/views/TripPreview.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { formatReferenceDuration, mapV4Preview } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const mixed = mapV4Preview(mixedSyntheticEnvelope())
const day = mixed.days[0]
assert.equal(mixed.cost.knownSubtotal, '90', 'display the backend subtotal without recalculating it')
assert.equal(day.cost.unknownCount, 1)
assert.equal(day.activities[1].cost.amount, null, 'unknown cost must remain unknown, not zero')
assert.match(day.activities[1].cost.text, /未查询到/)
assert.match(day.activities[0].cost.evidenceLabel, /高德地图.*查询于/)
assert.equal(mixed.planIssues.length, 1, 'global must-visit issue stays visible')
assert.ok(day.activities[0].issues.some(issue => issue.issue_id === 'issue-activity-conflict'), 'activity issues stay beside their activity')
assert.ok(day.legs[0].issues.some(issue => issue.issue_id === 'issue-leg-unknown'), 'route issues stay beside their leg')
assert.ok(day.unboundIssues.some(issue => issue.issue_id === 'issue-unbound-activity'), 'unbound issues fall back to the day notice area')
assert.deepEqual(day.missingSlots.map(slot => [slot.period, slot.kind]), [['breakfast', 'conditional'], ['evening', 'missing']])
assert.match(day.missingSlots[0].prompt, /待确认/)
assert.match(day.missingSlots[1].prompt, /尚未安排景点/)
assert.equal(day.returnTiming.status, 'unverified')
assert.equal(day.returnLeg.id, 'leg-return', 'departure route stays available as a separate return leg')
assert.equal(day.returnLeg.modes.length, 3, 'return route displays unified public transport, cycling, and walking')
assert.ok(day.returnLeg.issues.some(issue => issue.issue_id === 'issue-return-conflict'))
assert.equal(day.returnTiming.bufferLabel, '返程预留 1小时0分钟')
assert.match(day.returnTiming.detail, /未提供离开地点/)
assert.equal(day.legs[0].modes.find(mode => mode.mode === 'bicycling').durationSeconds, 420, 'non-selected modes use their shortest valid option')
assert.equal(day.legs[0].paths.length, 1, 'draw only geometry from the selected route option')
assert.match(day.legs[0].timeBasisLabel, /公共交通按.*09:00 参考时刻查询/)
assert.equal(day.recommendations.filter(candidate => candidate.kind === 'sightseeing').length, 2)
assert.equal(day.recommendations.filter(candidate => candidate.kind === 'meal').length, 1)
assert.ok(!day.recommendations.some(candidate => candidate.id === 'candidate-selected-sight'))
assert.equal(formatReferenceDuration(1800), '0小时30分钟')

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
  duration_s: 1800,
  duration_source: 'default',
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
assert.equal(short.days[0].returnTiming.status, 'conflict')

const transitionsEnvelope = mixedSyntheticEnvelope()
transitionsEnvelope.timing.days[0].unknown_transitions = [
  { leg_id: null, reason: 'missing_route_leg' },
  { leg_id: null, reason: 'missing_route_leg' },
  { leg_id: 'leg-known', reason: 'missing_route_leg' },
  { leg_id: 'leg-known', reason: 'missing_route_leg' },
]
transitionsEnvelope.timing.days[0].summary.unknown_leg_ids = ['leg-known', 'leg-summary-only']
assert.equal(mapV4Preview(transitionsEnvelope).days[0].timing.unknownLegCount, 4, 'count id-less transitions, deduplicate named transition ids, then add only summary ids not covered by a named transition')

const partialPath = '/private/tmp/trip-remaining-review/f10b-partial-envelope.json'
try {
  await access(partialPath)
  const partialEnvelope = JSON.parse(await readFile(partialPath, 'utf8'))
  const partial = mapV4Preview(partialEnvelope)
  assert.equal(partial.city, '上海')
  assert.equal(partial.days[0].timing.unknownLegCount, 2, 'id-less unknown transitions must not disappear when summary ids are empty')
  assert.ok(partial.cost.knownSubtotal === null || partial.cost.knownSubtotal === '0')
  assert.ok(partial.days[0].activities.length <= 2, 'the supplied fact sample remains visibly partial')
  assert.ok(partial.days[0].activities.every(activity => activity.cost.amount === null), 'the supplied all-unknown cost sample must not invent amounts')
  console.log('v4PreviewMapper: actual partial envelope boundary checks passed')
} catch (error) {
  if (error?.code !== 'ENOENT') throw error
  console.log('v4PreviewMapper: external partial envelope not present; skipped environment-only sample')
}

console.log('v4PreviewMapper: mixed synthetic and short-departure assertions passed')
