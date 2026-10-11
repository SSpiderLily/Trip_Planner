import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { mixedSyntheticEnvelope } from './v4PreviewFixtures.mjs'

const source = await readFile(new URL('../src/utils/v4PreviewEditController.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const controller = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const mapperSource = await readFile(new URL('../src/utils/v4PreviewMapper.ts', import.meta.url), 'utf8')
const mapperCompiled = ts.transpileModule(mapperSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { mapV4Preview } = await import(`data:text/javascript;base64,${Buffer.from(mapperCompiled).toString('base64')}`)
const destinationSource = await readFile(new URL('../src/utils/v4PreviewEditMapper.ts', import.meta.url), 'utf8')
const destinationCompiled = ts.transpileModule(destinationSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { groupV4DestinationActivities } = await import(`data:text/javascript;base64,${Buffer.from(destinationCompiled).toString('base64')}`)

const memory = new Map()
globalThis.localStorage = {
  getItem: key => memory.get(key) ?? null,
  setItem: (key, value) => memory.set(key, value),
  removeItem: key => memory.delete(key),
}

const envelope = mixedSyntheticEnvelope()
const groupedEnvelope = mixedSyntheticEnvelope()
const groupedDraft = groupedEnvelope.preview.draft.days[0]
const [morning, meal, afternoon] = groupedDraft.activities
groupedDraft.activities = [morning, meal, {
  ...afternoon,
  poi_id: morning.poi_id,
  candidate_ref: null,
  slot_id: groupedEnvelope.preview.planning_input.days[0].slots.find(slot => slot.period === 'afternoon').slot_id,
}]
const groupedView = mapV4Preview(groupedEnvelope).days[0]
const groups = groupV4DestinationActivities([
  ...groupedView.activities,
  { ...groupedView.activities[0], id: 'activity-free', kind: 'free_time', poiId: null, candidateRef: null },
])
assert.equal(groups.length, 2, 'destination list groups repeated POIs and omits free-time blocks')
const repeated = groups.find(group => group.poiId === morning.poi_id)
assert.deepEqual(repeated.activityIds, [morning.activity_id, afternoon.activity_id])
assert.deepEqual(repeated.periodLabels, ['上午', '下午'])
assert.equal(repeated.candidateRef, morning.candidate_ref, 're-adding a destination keeps its known candidate reference')

const originalIds = envelope.preview.draft.days[0].activities.map(activity => activity.activity_id)
const operation = { type: 'reorder_activities', day_id: envelope.preview.draft.days[0].day_id, activity_ids: [...originalIds].reverse() }
const pending = controller.createV4PendingEdit({
  envelope,
  commandId: 'edit-original',
  operations: [operation],
  summary: '调整当天活动顺序',
  details: ['1. 下午安排', '2. 午餐安排', '3. 上午安排'],
})
assert.equal(pending.request.command.plan_id, envelope.plan_id)
assert.equal(pending.request.command.base_revision, envelope.plan_revision)
assert.equal(pending.request.expected_store_version, envelope.store_version)
assert.deepEqual(pending.request.command.operations, [operation])

controller.persistV4PendingEdit(pending)
assert.equal(controller.hasStoredV4PendingEdit(envelope.task_id), true)
assert.deepEqual(controller.readV4PendingEdit(envelope.task_id), pending,
  'refresh recovery must retain the exact request, command id, and visible intent')
assert.equal(controller.markV4PendingEdit(pending, 'retry', 'network unavailable').request.command.command_id,
  'edit-original', 'retry keeps the idempotency key unless the user explicitly rebases a conflict')

const latest = structuredClone(envelope)
latest.store_version += 1
latest.plan_revision += 1
latest.preview.draft.revision += 1
const day = latest.preview.draft.days[0]
day.activities = [day.activities[0], day.activities[2]]
day.activities.push({ ...day.activities[0], activity_id: 'activity-added-elsewhere' })
const rebased = controller.rebaseV4PendingEdit(pending, latest, 'edit-rebased')
assert.equal(rebased.request.command.command_id, 'edit-rebased')
assert.equal(rebased.request.expected_store_version, latest.store_version)
assert.equal(rebased.request.command.base_revision, latest.plan_revision)
assert.deepEqual(rebased.request.command.operations[0].activity_ids,
  [originalIds[2], originalIds[0], 'activity-added-elsewhere'],
  'a deliberate retry preserves the chosen relative order and appends activities added since the conflict')

const changed = controller.rebaseV4PendingEdit(pending, latest, 'edit-rebased-again')
assert.notEqual(changed.request.command.command_id, pending.request.command.command_id)
assert.equal(changed.details.join(','), pending.details.join(','))

const twentyOne = Array.from({ length: 21 }, (_, index) => ({
  type: 'remove_activity', day_id: day.day_id, activity_id: `activity-${index}`,
}))
assert.throws(() => controller.createV4PendingEdit({ envelope, commandId: 'too-many', operations: twentyOne, summary: '批量删除' }), /最多包含 20 项/)
assert.throws(() => controller.createV4PendingEdit({
  envelope,
  commandId: 'multi-day',
  operations: [operation, { ...operation, day_id: 'another-day' }],
  summary: '跨日编辑',
}), /只能编辑同一天/)

memory.set(`trip_planner_v4_pending_edit_v1:${encodeURIComponent(envelope.task_id)}`, '{bad json')
assert.equal(controller.hasStoredV4PendingEdit(envelope.task_id), true)
assert.equal(controller.readV4PendingEdit(envelope.task_id), null)
memory.set(`trip_planner_v4_pending_edit_v1:${encodeURIComponent(envelope.task_id)}`, JSON.stringify({
  ...pending,
  request: { command: { command_id: 'incomplete' }, expected_store_version: 1 },
}))
assert.equal(controller.readV4PendingEdit(envelope.task_id), null, 'structurally invalid browser data is never sent as an edit')
controller.clearV4PendingEdit(envelope.task_id)
assert.equal(controller.hasStoredV4PendingEdit(envelope.task_id), false)

const access = { taskId: envelope.task_id, planId: envelope.plan_id, accessToken: 'fixture-token' }
const refreshed = structuredClone(envelope)
refreshed.store_version += 1
refreshed.plan_revision += 1
refreshed.preview.draft.revision += 1
assert.equal(controller.canAdoptV4DiscardRefresh(access, refreshed, envelope), true)
assert.equal(controller.canAdoptV4DiscardRefresh(access, envelope, refreshed), false,
  'discard must reject a server response older than the visible preview')
assert.equal(controller.canAdoptV4DiscardRefresh({ ...access, planId: 'another-plan' }, refreshed, envelope), false,
  'discard must reject a response for another plan')

const discardEvents = []
const discarded = await controller.refreshBeforeDiscardV4PendingEdit({
  access,
  current: envelope,
  readLatest: async () => { discardEvents.push('read'); return refreshed },
  adopt: latestEnvelope => { assert.equal(latestEnvelope, refreshed); discardEvents.push('adopt') },
  clear: () => { discardEvents.push('clear'); return true },
})
assert.equal(discarded, 'discarded')
assert.deepEqual(discardEvents, ['read', 'adopt', 'clear'],
  'discard reads and adopts the latest server result before clearing the retry command')
let failedReadCleared = false
await assert.rejects(() => controller.refreshBeforeDiscardV4PendingEdit({
  access,
  current: envelope,
  readLatest: async () => { throw new Error('offline') },
  adopt: () => assert.fail('failed reads cannot replace the visible draft'),
  clear: () => { failedReadCleared = true; return true },
}))
assert.equal(failedReadCleared, false, 'a failed refresh retains the exact pending command')
let staleDiscardCleared = false
const staleDiscard = await controller.refreshBeforeDiscardV4PendingEdit({
  access,
  current: refreshed,
  readLatest: async () => envelope,
  adopt: () => assert.fail('older server state cannot replace the visible draft'),
  clear: () => { staleDiscardCleared = true; return true },
})
assert.equal(staleDiscard, 'stale')
assert.equal(staleDiscardCleared, false)

const routeEnvelope = structuredClone(envelope)
routeEnvelope.preview.calculation.current_routes = 'pending'
routeEnvelope.preview.calculation.calculation_id = 'edit:initial'
assert.equal(controller.pendingV4RouteJobId(routeEnvelope), 'edit:initial')
const supersedingRouteEnvelope = structuredClone(routeEnvelope)
supersedingRouteEnvelope.store_version += 1
supersedingRouteEnvelope.plan_revision += 1
supersedingRouteEnvelope.preview.draft.revision += 1
supersedingRouteEnvelope.preview.calculation.calculation_id = 'edit:newer'
assert.equal(controller.pendingV4RouteJobId(supersedingRouteEnvelope), 'edit:newer',
  'a superseded response can hand polling to its newer pending calculation')
assert.equal(controller.nextV4RouteJobAfterSuperseded(
  'edit:initial', supersedingRouteEnvelope, routeEnvelope,
), 'edit:newer', 'polling follows the new calculation supplied with superseded status')
assert.equal(controller.nextV4RouteJobAfterSuperseded(
  'edit:initial', routeEnvelope, supersedingRouteEnvelope,
), null, 'a stale superseded response cannot move polling backward')
assert.equal(controller.nextV4RouteJobAfterSuperseded(
  'edit:newer', supersedingRouteEnvelope, routeEnvelope,
), null, 'a superseded response for the same pending calculation does not loop')
supersedingRouteEnvelope.preview.calculation.calculation_id = 'edit:done'
supersedingRouteEnvelope.preview.calculation.current_routes = 'completed'
assert.equal(controller.pendingV4RouteJobId(supersedingRouteEnvelope), null,
  'a non-pending calculation clears the old route-job banner')
assert.equal(controller.nextV4RouteJobAfterSuperseded(
  'edit:initial', supersedingRouteEnvelope, routeEnvelope,
), null, 'a superseded response with no pending routes ends the old poll')

controller.persistV4PendingEdit(pending)
const removeItem = globalThis.localStorage.removeItem
globalThis.localStorage.removeItem = () => { throw new Error('storage unavailable') }
assert.equal(controller.clearV4PendingEdit(envelope.task_id), false, 'a failed discard must remain visible')
assert.equal(controller.hasStoredV4PendingEdit(envelope.task_id), true)
globalThis.localStorage.removeItem = removeItem
controller.clearV4PendingEdit(envelope.task_id)

console.log('v4PreviewEditController: exact recovery, conflict rebase, discard refresh ordering, route-job supersession, and cache boundaries passed')
