import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const url = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const transpile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const nativeSource = await readFile(new URL('../src/services/nativeObservationView.ts', import.meta.url), 'utf8')
const nativeURL = url(transpile(nativeSource)), native = await import(nativeURL)
const viewURL = url(transpile(await readFile(new URL('../src/services/observationView.ts', import.meta.url), 'utf8')))
const apiURL = url(`import {nativeStepNames} from '${nativeURL}'; export const stepName = name => nativeStepNames[name] || name`)
const source = await readFile(new URL('../src/components/ObservationDetails.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'native-observation-fixture', inlineTemplate: true })
let compiled = transpile(script.content)
for (const [module, target] of [['vue', import.meta.resolve('vue')], ['@/services/api', apiURL], ['@/services/observationView', viewURL], ['@/services/nativeObservationView', nativeURL]]) {
  compiled = compiled.replaceAll(`from "${module}"`, `from "${target}"`).replaceAll(`from '${module}'`, `from '${target}'`)
}
const component = (await import(url(compiled))).default
const fixture = (event, output, metadata = {}) => ({name: `native.${event}`, span_id: 'mock-span', operation_type: 'agent', status: 'succeeded',
  started_at: '2026-10-13T00:00:00Z', finished_at: '2026-10-13T00:00:00Z', input_data: {native_event: event, ...metadata}, output_data: output,
  input_truncated: false, output_truncated: false, input_tokens: null, output_tokens: null, total_tokens: null, error: null})
const fixtures = [
  [fixture('master.task', {round: 2, arguments: {city: '上海', dates: ['2026-10-20'], objective: '查询午餐'}}, {role: 'master', target_role: 'dining', subtask_id: 'mock-food'}), ['主智能体派发子任务', '主智能体', '派发对象：餐饮子智能体', '查询午餐', 'mock-food', '2026-10-20']],
  [fixture('map.request', {tool_name: 'maps_around_search', arguments: {keywords: '餐馆', radius: 1500}}, {role:'dining'}), ['子智能体发起高德查询', '餐饮子智能体', 'maps_around_search', '1500']],
  [fixture('child.result', {task_result: {candidates: [{candidate_id:'mock-candidate'}]}, places:[{name:'模拟餐馆'}]}, {role:'dining'}), ['子智能体返回候选与缺项', '返回候选：1 个', '模拟餐馆', 'mock-candidate']],
  [fixture('planning.initial', {draft: {revision: 1, days:[{theme:'模拟初版'}]}, report: {issues:[]}}), ['每日行程初版与校验', '行程版本：1', '每日安排：1 天', '模拟初版']],
  [fixture('repair.revision', {round:1, revision: {revision:2, replacement_day: {theme:'模拟修订'}}}), ['主智能体提交单日修订', '行程版本：2', '模拟修订']],
  [fixture('repair.submission_rejected', {outcome:'rejected', codes:['REVISION_INVALID']}), ['单日修订未通过', '业务提交未通过', 'REVISION_INVALID']],
  [fixture('tool.arguments_rejected', {outcome:'rejected', role:'master', stage:'daily_plan', code:'INVALID_TOOL_ARGUMENTS', tool_name:'submit_daily_plan', issues:[{path:['days',0,'activities',0],type:'value_error'}]}), ['工具参数未通过', '主智能体', '阶段：每日规划', 'INVALID_TOOL_ARGUMENTS', 'submit_daily_plan', 'value_error']],
]
for (const [detail, expected] of fixtures) {
  const html = await renderToString(createSSRApp(component, {detail, spanId:detail.span_id, loading:false, error:'', active:false, now:0}))
  for (const text of expected) assert.ok(html.includes(text), `Rendered detail missing ${text}`)
}
assert.deepEqual(native.nativeObservationSummary(null), [])
assert.deepEqual(native.nativeObservationSummary({name:'legacy.step'}), [])
assert.ok(native.nativeObservationSummary(fixture('map.result', {returned_places:[]}, {})).some(x => x.includes('0 个')))
assert.ok(!native.nativeObservationSummary({...fixture('planning.initial', {}), output_data:{preview:'truncated'}}).some(x=>x.includes('每日安排：0')))
console.log('nativeObservation: 7 actual Vue detail renders and 4 boundary checks passed')
