import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { ref, computed, nextTick } from 'vue'
const source = await readFile(process.env.OBS_COMPONENT_SOURCE || new URL('../src/views/Observability.vue', import.meta.url), 'utf8')
const helper = await readFile(new URL('../src/services/observationView.ts', import.meta.url), 'utf8')
const compile = value => ts.transpileModule(value, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const helpers = await import(`data:text/javascript;base64,${Buffer.from(compile(helper)).toString('base64')}`)
const body = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '')
const deps = ['ref','computed','nextTick','watch','onMounted','onUnmounted','useRoute','useRouter','window','setTimeout','clearTimeout','setInterval','clearInterval','listTasks','getTask','getSpans','getSpan','getTaskResult','getTaskMetrics','errorText','stepName','stateName','getPreviewAccessForTask','recoverLocalPreviewAccess','getV4ObservationMetrics','getV4ObservationSpans','getV4ObservationSpan','observationErrorText','anomalyCounts','defaultSelection','durationMs','isActive','milliseconds','normalizeSpan','time','tokenText']
const wrapped = `export function create(deps) { const {${deps.join(',')}} = deps; ${body}; return {loadTask,loadMetrics,loadList,showResult,clearTask,selected,metrics,spans,detail,taskError,selectedId}; }`
const { create } = await import(`data:text/javascript;base64,${Buffer.from(compile(wrapped)).toString('base64')}`)
const task = { task_id:'task-a',status:'running',created_at:'2026-10-26T00:00:00Z',started_at:'2026-10-26T00:00:00Z',finished_at:null,observation_incomplete:false }
const span = { span_id:'span-a',task_id:'task-a',parent_span_id:null,name:'llm.native_tool_loop',operation_type:'llm',status:'running',started_at:task.created_at,finished_at:null,input_tokens:null,output_tokens:null,total_tokens:null,role_name:'主智能体' }
function fixture({ host='production.example', access=true, fail=null, read=null }={}) {
  const calls=[], paths=[]; let mounted
  const auth = {taskId:'task-a',planId:'plan-a',accessToken:'synthetic-bearer-a'}
  const result = {task,model:{total:1},tool:{total:0},tokens:{total_tokens:null,total_calls:1,covered_calls:0,complete:false}}
  const legacy = name => async (...args) => {calls.push([name,...args]);throw new Error('legacy endpoint must not be used')}
  const scoped = (name,value) => async (...args) => {calls.push([name,...args]);assert.equal(args[0].taskId,'task-a');if(fail)throw fail;return name==='metrics'&&read?read():value}
  const state=create({...helpers,ref,computed,nextTick,watch:()=>{},onMounted:fn=>mounted=fn,onUnmounted:()=>{},
    useRoute:()=>({query:{task:'task-a',view:'trace'}}), useRouter:()=>({push:async p=>paths.push(p),replace:async()=>{}}),
    window:{location:{hostname:host},matchMedia:()=>({matches:false})},setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>2,clearInterval:()=>{},
    listTasks:legacy('list'),getTask:legacy('task'),getSpans:legacy('spans'),getSpan:legacy('detail'),getTaskResult:legacy('result'),getTaskMetrics:legacy('legacy-metrics'),errorText:()=> 'legacy error',stepName:n=>n,stateName:n=>n,
    getPreviewAccessForTask:id=>access&&id==='task-a'?auth:null,recoverLocalPreviewAccess:legacy('recovery'),
    getV4ObservationMetrics:scoped('metrics',result),getV4ObservationSpans:scoped('scoped-spans',[span]),getV4ObservationSpan:scoped('scoped-detail',{...span,input_data:{role:'master'}})})
  state.clearTask('task-a');return {...state,calls,paths,mount:()=>mounted()}
}
const running=fixture();running.mount();await running.loadTask();await Promise.resolve()
assert.equal(running.selected.value.status,'running');assert.equal(running.metrics.value.model.total,1)
assert.equal(running.spans.value[0].span_id,'span-a');assert.equal(running.detail.value.input_data.role,'master')
assert.ok(running.calls.every(([name])=>['metrics','scoped-spans','scoped-detail'].includes(name)))
await running.showResult();assert.deepEqual(running.paths,[{path:'/preview',query:{task:'task-a'}}])
const missing=fixture({access:false});missing.mount();await missing.loadTask()
assert.equal(missing.calls.length,0);assert.match(missing.taskError.value,/缺少.*凭据/)
const absent=fixture();absent.clearTask('');absent.mount();await absent.loadTask();assert.equal(absent.calls.length,0)
const denied=fixture({fail:{response:{status:403,data:{detail:{code:'TASK_ACCESS_DENIED'}}}}});await denied.loadTask()
assert.equal(denied.selected.value,null);assert.equal(denied.metrics.value,null);assert.match(denied.taskError.value,/无法访问/)
const proxy404=fixture({fail:{response:{status:404,data:'<html>404</html>'}}});await proxy404.loadTask()
assert.equal(proxy404.selectedId.value,'task-a');assert.match(proxy404.taskError.value,/接口/)
let resolve
const stale=fixture({read:()=>new Promise(done=>resolve=done)});const pending=stale.loadTask();stale.clearTask('task-b');resolve({task,model:{total:1}});await pending
assert.equal(stale.selectedId.value,'task-b');assert.equal(stale.selected.value,null)
console.log('taskScopedObservation: public current-task reads, missing credentials, proxy errors, result entry and stale responses passed')
