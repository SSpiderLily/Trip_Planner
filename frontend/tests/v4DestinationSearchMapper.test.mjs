import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/v4DestinationSearchMapper.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const mapper = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const candidate = {
  candidate_token: 'private-candidate-token-12345678901234567890',
  name: '街角小馆',
  address: '上海市南京东路',
  city: '上海市',
  location: { lng: 121.49, lat: 31.24, coordinate_system: 'GCJ-02' },
  type_name: '餐饮服务',
  cost: { raw: null, value: null, unit: 'per_person_reference' },
  opening_hours_raw: null,
  photos: [],
  missing_fields: [{ field: 'cost', reason: 'not_returned' }],
  source: {
    provider: 'amap',
    tool_name: 'internal_search_tool_name',
    api_version: 'v4',
    queried_at: '2026-10-11T03:20:00Z',
    effective_params: { keywords: '街角小馆' },
  },
  selectable: true,
  selection_block_reason: null,
}

const mapped = mapper.mapV4DestinationSearchCandidate(candidate, 0)
assert.equal(mapped.costLabel, '尚未查到价格')
assert.equal(mapped.sourceLabel, '高德地图')
assert.match(mapped.queriedAtLabel, /2026/)
assert.equal(mapped.canSelect, true)
assert.equal(mapped.missingFieldsLabel, '部分信息未提供')
assert.equal(JSON.stringify(mapped).includes(candidate.candidate_token), false,
  'opaque selection tokens never enter display models')
assert.equal(JSON.stringify(mapped).includes(candidate.source.tool_name), false,
  'tool identifiers are not exposed to users')

const knownCost = mapper.mapV4DestinationSearchCandidate({
  ...candidate,
  cost: { raw: '人均约50元', value: '50', unit: 'per_person_reference' },
  selectable: false,
  candidate_token: null,
  selection_block_reason: 'location_unavailable',
}, 1)
assert.equal(knownCost.costLabel, '人均参考消费：¥50')
assert.equal(knownCost.canSelect, false)
assert.equal(knownCost.blockedReasonLabel, '没有可用位置，暂时不能加入')

const expiredSelection = mapper.mapV4DestinationSelectFailure({
  status: 422,
  code: 'DESTINATION_CANDIDATE_UNAVAILABLE',
  unavailable: false,
})
assert.match(expiredSelection, /已失效/)
assert.match(expiredSelection, /重新搜索/)
assert.match(expiredSelection, /停止重试并刷新/,
  'an expired candidate keeps a clear path to stop retrying and refresh server state')
const serviceUnavailable = mapper.mapV4DestinationSelectFailure({ status: 503, code: null, unavailable: false })
assert.match(serviceUnavailable, /尚未确认地点是否加入/)
assert.doesNotMatch(serviceUnavailable, /地点暂未加入/,
  'a lost response may follow a server commit, so an unavailable response must not claim rejection')
assert.match(serviceUnavailable, /同一命令核对结果/)
assert.match(mapper.mapV4DestinationSelectFailure({ status: null, code: null, unavailable: false }), /尚未确认地点是否加入/)
assert.match(mapper.mapV4DestinationSelectFailure({ status: null, code: null, unavailable: true }), /尚未确认地点是否加入/)

console.log('v4DestinationSearchMapper: user-facing source/cost and opaque-token boundaries passed')
