import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const compile = text => ts.transpileModule(text,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText
const asUrl = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`
const calls=[];globalThis.__observationRequestCapture=(url,config)=>{calls.push({url,config});return {data:[]}}
const fakeAxios=asUrl('export default {create(){return {get:async(url,config)=>globalThis.__observationRequestCapture(url,config)}}}')
const mapper=asUrl(compile(await readFile(new URL('../src/utils/v4DestinationSearchMapper.ts',import.meta.url),'utf8')))
const source=compile(await readFile(new URL('../src/services/v4Preview.ts',import.meta.url),'utf8'))
  .replace("from 'axios'",`from '${fakeAxios}'`).replace("from '@/utils/v4DestinationSearchMapper'",`from '${mapper}'`)
  .replace("import.meta.env.VITE_API_BASE_URL", "'/trip'")
const api=await import(asUrl(source))
const access={taskId:'task /a',planId:'plan-a',accessToken:'synthetic-header-only'}
await api.getV4ObservationMetrics(access);await api.getV4ObservationSpans(access);await api.getV4ObservationSpan(access,'span /a')
assert.deepEqual(calls.map(x=>x.url),['/api/trip/v4/tasks/task%20%2Fa/metrics','/api/trip/v4/tasks/task%20%2Fa/spans','/api/trip/v4/tasks/task%20%2Fa/spans/span%20%2Fa'])
for(const {url,config} of calls){assert.equal(config.headers.Authorization,'Bearer synthetic-header-only');assert.ok(!url.includes(access.accessToken));assert.ok(!url.includes('plan-a'))}
delete globalThis.__observationRequestCapture
console.log('taskScopedObservationRequests: all three actual service functions use task Bearer headers and encoded IDs without URL tokens')
