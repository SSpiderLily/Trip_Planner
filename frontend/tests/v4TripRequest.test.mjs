import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const utilitySource = await readFile(new URL('../src/utils/v4TripRequest.ts', import.meta.url), 'utf8')
const pageSource = await readFile(new URL('../src/views/TripPlan.vue', import.meta.url), 'utf8')
const routeSource = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const appSource = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')
const serviceSource = await readFile(new URL('../src/services/v4TripTasks.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(utilitySource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const { buildV4TripRequest, createSingleFlightSubmitter } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`,
)

const request = buildV4TripRequest({
  city: ' 上海 ',
  arrivalLocal: '2026-11-05T08:30',
  departureLocal: '2026-11-07T18:10',
  budgetAmount: '5200',
  lodgingQuery: ' 浦东新区世纪大道附近 ',
  arrivalPlaceQuery: ' 上海虹桥站 ',
  departurePlaceQuery: '',
  returnBufferHours: null,
  returnBufferMinutes: null,
  preferences: [' 美食 ', '美食', '', '历史文化'],
  remarks: '  想慢慢逛老街  ',
}, 'request_test_1')

assert.equal(request.schema_version, 4)
assert.equal(request.request_id, 'request_test_1')
assert.equal(request.city, '上海')
assert.equal(request.arrival_at, '2026-11-05T08:30:00+08:00', 'date picker wall time is encoded as Shanghai time')
assert.equal(request.departure_at, '2026-11-07T18:10:00+08:00')
assert.equal(request.timezone, 'Asia/Shanghai')
assert.deepEqual(request.preferences, ['美食', '历史文化'])
assert.deepEqual(request.transport_preferences, ['transit', 'bicycling', 'walking'])
assert.equal(request.arrival_place_query, '上海虹桥站')
assert.equal(request.departure_place_query, null)
assert.equal(request.lodging_query, '浦东新区世纪大道附近')
assert.equal(request.explicit_return_buffer_s, null, 'both blank buffer inputs preserve the backend default')
assert.deepEqual(request.budget, {
  amount: 5200,
  currency: 'CNY',
  scope: 'whole_trip',
  party_basis: 'unspecified',
  party_size: null,
})
assert.equal(request.remarks, '想慢慢逛老街')

const explicitZero = buildV4TripRequest({
  city: '成都', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00',
  budgetAmount: null, lodgingQuery: '', arrivalPlaceQuery: '', departurePlaceQuery: '',
  returnBufferHours: 0, returnBufferMinutes: 0, preferences: [], remarks: '',
}, 'request_zero')
assert.equal(explicitZero.explicit_return_buffer_s, 0, 'explicit zero must not be converted to null')
const minutesOnly = buildV4TripRequest({
  city: '成都', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00',
  budgetAmount: undefined, lodgingQuery: '', arrivalPlaceQuery: '', departurePlaceQuery: '',
  returnBufferHours: null, returnBufferMinutes: 3, preferences: [], remarks: '',
}, 'request_minutes')
assert.equal(minutesOnly.explicit_return_buffer_s, 180)

const invalidInputs = [
  [{ city: ' ', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00' }, /城市/],
  [{ city: '上海', arrivalLocal: null, departureLocal: '2026-11-06T18:10:00' }, /到达日期/],
  [{ city: '上海', arrivalLocal: '2026-02-30T08:30:00', departureLocal: '2026-03-01T18:10:00' }, /到达日期/],
  [{ city: '上海', arrivalLocal: '2026-11-06T18:10:00', departureLocal: '2026-11-06T18:10:00' }, /晚于/],
  [{ city: '上海', arrivalLocal: '2026-11-07T18:10:00', departureLocal: '2026-11-06T18:10:00' }, /晚于/],
]
for (const [partial, errorPattern] of invalidInputs) {
  const base = {
    city: '上海', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00',
    budgetAmount: null, lodgingQuery: '', arrivalPlaceQuery: '', departurePlaceQuery: '',
    returnBufferHours: null, returnBufferMinutes: null, preferences: [], remarks: '',
    ...partial,
  }
  assert.throws(() => buildV4TripRequest(base, 'request_invalid'), errorPattern)
}
assert.throws(() => buildV4TripRequest({
  city: '上海', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00',
  budgetAmount: null, lodgingQuery: '', arrivalPlaceQuery: '', departurePlaceQuery: '',
  returnBufferHours: null, returnBufferMinutes: 60, preferences: [], remarks: '',
}, 'request_invalid_minute'), /分钟数/)
assert.throws(() => buildV4TripRequest({
  city: '上海', arrivalLocal: '2026-11-05T08:30:00', departureLocal: '2026-11-06T18:10:00',
  budgetAmount: -1, lodgingQuery: '', arrivalPlaceQuery: '', departurePlaceQuery: '',
  returnBufferHours: null, returnBufferMinutes: null, preferences: [], remarks: '',
}, 'request_invalid_budget'), /预算/)

let sendCount = 0
let releaseSend
const delayedSend = createSingleFlightSubmitter(async value => {
  sendCount += 1
  await new Promise(resolve => { releaseSend = resolve })
  return { task_id: 'task-1', input: value }
})
const firstSubmit = delayedSend(request)
const duplicateSubmit = delayedSend({ ...request, city: '重复点击' })
assert.equal(firstSubmit, duplicateSubmit, 'concurrent form submits share one network operation')
assert.equal(sendCount, 1)
releaseSend()
const [firstResult, duplicateResult] = await Promise.all([firstSubmit, duplicateSubmit])
assert.equal(firstResult, duplicateResult)
assert.equal((await delayedSend(request)).task_id, 'task-1', 'an accepted response is never submitted again by this form')
assert.equal(sendCount, 1)

assert.match(serviceSource, /\/api\/trip\/v4\/tasks/)
assert.doesNotMatch(serviceSource, /console\.(log|error)|interceptors\.request/)
assert.match(pageSource, /savePreviewAccess\(access\.taskId, access\.accessToken, access\.planId\)/)
assert.match(pageSource, /continueToPreview\(\)/)
assert.match(pageSource, /if \(submitting\.value \|\| acceptedAccess\.value\) return/)
assert.match(pageSource, /继续上次预览/)
assert.match(pageSource, /getLatestPreviewAccess\(\)/)
assert.match(routeSource, /path: '\/plan'/)
assert.match(appSource, /route\.path === '\/preview'.*to: '\/plan'/)
assert.match(appSource, /route\.path === '\/plan'.*to: '\/plan#plan-form-title'/)
assert.match(appSource, /to: '\/#plan', label: '开始规划'/, 'the default v3 home keeps its original planner link')
assert.match(pageSource, /例如：上海、成都/)
assert.doesNotMatch(pageSource, /京都|巴黎|无障碍设施/)
assert.doesNotMatch(pageSource, /· V4|v4规划任务|后端会核实|Asia\/Shanghai/)
console.log('v4TripRequest: request mapping, invalid dates, buffer semantics and duplicate-submit boundary passed')
