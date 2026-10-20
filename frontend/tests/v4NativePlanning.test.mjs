import assert from 'node:assert/strict'
import {test} from 'node:test'
import {readFile} from 'node:fs/promises'
import http from 'node:http'
import https from 'node:https'
import axios from 'axios'
import ts from 'typescript'
import {parse,compileScript} from '@vue/compiler-sfc'
import {createSSRApp} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {createRouter,createMemoryHistory} from 'vue-router'
import {mixedSyntheticEnvelope} from './v4PreviewFixtures.mjs'
const url=s=>`data:text/javascript;base64,${Buffer.from(s).toString('base64')}`
const compile=s=>ts.transpileModule(s,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText
const read=path=>readFile(new URL(path,import.meta.url),'utf8')
const progressURL=url(compile(await read('../src/utils/v4PreviewProgress.ts'))),progress=await import(progressURL)
const sessionURL=url(compile(await read('../src/utils/v4PlanningSession.ts')).replace("'./v4PreviewProgress'",`'${progressURL}'`)),session=await import(sessionURL)
const base=mixedSyntheticEnvelope();base.preview.draft.source_run_id=base.task_id
const access={taskId:base.task_id,planId:base.plan_id,accessToken:'mock-local-credential'}
const snapshot=()=>structuredClone(base)
const status=(stage='dispatch',state='running',extra={})=>({task_id:access.taskId,plan_id:access.planId,status:state,stage,preview_ready:state==='ready',store_version:state==='ready'?1:null,completed:0,total:1,message:null,error_code:null,backfill:{status:'not_started',completed:0,total:0},updated_at:'2026-10-13T00:00:00Z',...extra})
function reads(states,previews=[snapshot()]) {
 const stages=[],counts={status:0,preview:0,pause:0}
 return {stages,counts,isCurrent:()=>true,onProgress:(s,label)=>stages.push([s.stage,label]),pause:async ms=>{assert.equal(ms,1800);counts.pause++},readStatus:async()=>{counts.status++;return states.shift()},readPreview:async()=>{counts.preview++;const value=previews.shift();if(value instanceof Error||value?.response)throw value;return value}}
}
test('原生全阶段只读取状态，地点ready后交付，不等待交通',async()=>{
 const stages=['accepted','planning_input','dispatch','proposal','validation','repair','cost','ready'],envelope=snapshot();envelope.preview.calculation.current_routes='pending'
 const r=reads(stages.map(s=>status(s,s==='accepted'?'accepted':s==='ready'?'ready':'running')),[envelope])
 assert.equal(await session.waitForV4PlanningPreview(access,r),envelope);assert.deepEqual(r.stages.map(s=>s[0]),stages);assert.equal(r.counts.preview,1);assert.equal(r.counts.pause,7)
 assert.match(r.stages[1][1],/主智能体/);assert.match(r.stages[2][1],/景点、餐饮和住宿/);assert.match(r.stages[3][1],/候选地点/);assert.match(r.stages[5][1],/修订/)
})
test('缺项草稿和交通失败仍可预览',async()=>{
 const e=snapshot();e.preview.calculation.current_routes='failed';assert.ok(e.preview.draft.days[0].unfilled_slot_ids.length)
 assert.equal(await session.waitForV4PlanningPreview(access,reads([status('ready','ready',{backfill:{status:'failed',completed:0,total:1}})],[e])),e)
})
test('PREVIEW_NOT_READY竞态只重读GET',async()=>{
 const r=reads([status('ready','ready'),status('ready','ready')],[{response:{status:409,data:{detail:{code:'PREVIEW_NOT_READY'}}}},snapshot()])
 assert.ok(await session.waitForV4PlanningPreview(access,r));assert.equal(r.counts.preview,2);assert.equal(r.counts.pause,1)
})
test('模型、原生交付失败和中断使用安全文案，不读取预览',async()=>{
 for(const code of ['MODEL_CALL_FAILED','MASTER_DID_NOT_SUBMIT_REQUIREMENTS','NATIVE_PREVIEW_INVALID','INITIAL_COLLECTION_TIMEOUT']) {
  const r=reads([status('failed','failed',{error_code:code,message:'private-provider-response'})])
  await assert.rejects(session.waitForV4PlanningPreview(access,r),e=>e.action==='replan'&&!e.message.includes(code)&&!e.message.includes('private-provider-response'));assert.equal(r.counts.preview,0)
 }
 await assert.rejects(session.waitForV4PlanningPreview(access,reads([status('repair','interrupted')])),/已中断/)
})
test('错误身份不更新进度，不读取其他任务预览',async()=>{
 for(const field of ['task_id','plan_id']){const r=reads([status('proposal','running',{[field]:'another'})]);await assert.rejects(session.waitForV4PlanningPreview(access,r),/无法恢复/);assert.equal(r.stages.length,0);assert.equal(r.counts.preview,0)}
 for(const mutate of [e=>e.task_id='another',e=>e.preview.draft.source_run_id='another',e=>e.preview.draft.source_run_id='']){const e=snapshot();mutate(e);await assert.rejects(session.waitForV4PlanningPreview(access,reads([status('ready','ready')],[e])),/无法恢复/)}
})
test('错误交付状态、未知状态和旧版本要求重读，不误报ready',async()=>{
 for(const s of [status('ready','ready',{preview_ready:false}),status('dispatch','future'),status('ready','ready',{preview_ready:'yes'})])await assert.rejects(session.waitForV4PlanningPreview(access,reads([s])),e=>e.action==='read')
 for(const mutate of [e=>e.store_version=0,e=>e.plan_revision=99]){const e=snapshot();mutate(e);await assert.rejects(session.waitForV4PlanningPreview(access,reads([status('ready','ready')],[e])),e=>e.action==='read')}
 await assert.rejects(session.waitForV4PlanningPreview(access,reads([status('ready','ready',{store_version:2})],[snapshot()])),/版本尚未同步/)
})
test('切换或退出后丢弃迟到状态、预览和错误',async()=>{
 for(const point of ['status','preview','error']){let resolve,reject,active=true;const pending=new Promise((ok,fail)=>{resolve=ok;reject=fail}),r=reads([status('ready','ready')]);r.isCurrent=()=>active
  if(point==='preview')r.readPreview=()=>pending;else r.readStatus=()=>pending
  const result=session.waitForV4PlanningPreview(access,r);await Promise.resolve();active=false
  if(point==='error')reject(new Error('private-network-failure'));else resolve(point==='preview'?snapshot():status())
  assert.equal(await result,null)
 }
})
test('网络读取失败可重读，不重新提交规划',async()=>{const r=reads([]);r.readStatus=async()=>{throw new Error('network')};await assert.rejects(session.waitForV4PlanningPreview(access,r),/network/);assert.ok(await session.waitForV4PlanningPreview(access,reads([status('ready','ready')])) )})
test('观测入口只包含对应任务ID，不包含凭据',()=>{
 const target=session.planningObservationTarget(access.taskId);assert.deepEqual(target,{path:'/observability',query:{task:access.taskId,view:'trace'}});assert.ok(!JSON.stringify(target).includes(access.accessToken))
 for(const id of [null,undefined,'','   ',' task '])assert.equal(session.planningObservationTarget(id),null)
})
test('实际Vue进度组件和路由渲染各阶段及观测链接',async()=>{
 const {descriptor}=parse(await read('../src/components/preview/PlanningProgress.vue'));let compiled=compile(compileScript(descriptor,{id:'native-progress',inlineTemplate:true}).content)
 for(const [name,target] of [['vue',import.meta.resolve('vue')],['@/utils/v4PreviewProgress',progressURL],['@/utils/v4PlanningSession',sessionURL]])compiled=compiled.replaceAll(`from "${name}"`,`from "${target}"`).replaceAll(`from '${name}'`,`from '${target}'`)
 const component=(await import(url(compiled))).default
 for(const stage of ['planning_input','dispatch','proposal','validation','repair','cost','ready']){const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{template:'<div />'}},{path:'/observability',component:{template:'<div />'}}]});await router.push('/');await router.isReady()
  const app=createSSRApp(component,{stage,status:stage==='ready'?'ready':'running',taskId:access.taskId});app.use(router);const html=await renderToString(app)
  assert.ok(html.includes(progress.v4PreviewStageLabel(stage,stage==='ready'?'ready':'running')));assert.ok(html.includes(`/observability?task=${access.taskId}&amp;view=trace`));assert.ok(!html.includes(access.accessToken))
 }
})
test('实际客户端提交→轮询→预览→交通、编辑、保存和还原，HTTP全部模拟',async()=>{
 const requests=[],states=['planning_input','dispatch','proposal','repair','cost','ready'].map(s=>status(s,s==='ready'?'ready':'running'));let current=snapshot();current.preview.calculation.current_routes='pending'
 const originalCreate=axios.create,originalHttp=http.request,originalHttps=https.request,originalFetch=globalThis.fetch;let networkAttempts=0
 const forbid=()=>{networkAttempts++;throw new Error('禁止真实网络')};http.request=https.request=globalThis.fetch=forbid
 axios.create=config=>originalCreate({...config,adapter:async config=>{const path=config.url,method=config.method,body=typeof config.data==='string'?JSON.parse(config.data):config.data;requests.push({path,method,body,authorized:config.headers.Authorization===`Bearer ${access.accessToken}`});let data
  if(method==='post'&&path==='/api/trip/v4/tasks')data={task_id:access.taskId,plan_id:access.planId,access_token:access.accessToken,status:'accepted'}
  else if(path.endsWith('/preview'))data=structuredClone(current)
  else if(path.includes('/edit-jobs/')){current.preview.calculation.current_routes='completed';data={envelope:structuredClone(current),job:{status:'completed'}}}
  else if(path.endsWith('/edits')){current.store_version++;current.plan_revision++;current.preview.draft.revision++;data={envelope:structuredClone(current),job:null}}
  else if(path.endsWith('/final/save'))data={status:'saved',restorable:true}
  else if(path.endsWith('/final/restore'))data={envelope:structuredClone(current),restored_from:{save_id:'mock-save'}}
  else data=states.shift()
  return {status:path==='/api/trip/v4/tasks'?202:200,statusText:'OK',headers:{},config,data}
 }})
 try{
  const importService=async path=>{let compiled=compile((await read(path)).replaceAll('import.meta.env','({VITE_API_BASE_URL:"http://mock-only"})'));const mapperURL=url(compile(await read('../src/utils/v4DestinationSearchMapper.ts')));compiled=compiled.replace("'axios'",`'${import.meta.resolve('axios')}'`).replace("'@/utils/v4DestinationSearchMapper'",`'${mapperURL}'`);return import(url(compiled))}
  const tasks=await importService('../src/services/v4TripTasks.ts'),api=await importService('../src/services/v4Preview.ts'),accepted=await tasks.submitV4TripTask({...base.preview.planning_input.request,request_id:'mock-request'});assert.equal(accepted.status,'accepted')
  const labels=[],delivered=await session.waitForV4PlanningPreview(access,{readStatus:api.getV4TaskStatus,readPreview:api.getV4Preview,onProgress:(s,label)=>labels.push(label),isCurrent:()=>true,pause:async()=>{}});assert.equal(delivered.preview.calculation.current_routes,'pending');assert.equal(labels.length,6)
  const mapper=await import(url(compile(await read('../src/utils/v4PreviewMapper.ts'))));assert.equal(mapper.mapV4Preview(delivered).city,'上海')
  assert.equal((await api.getV4EditJob(access,'mock-calculation')).job.status,'completed')
  assert.equal((await api.submitV4Edit(access,{expected_store_version:1,command:{plan_id:access.planId,base_revision:1,command_id:'mock-edit',day_id:base.preview.draft.days[0].day_id,operations:[{type:'remove_activity',day_id:base.preview.draft.days[0].day_id,activity_id:base.preview.draft.days[0].activities[0].activity_id}]}})).envelope.plan_revision,2)
  assert.equal((await api.saveV4Final(access,{save_id:'mock-save',source_store_version:2})).status,'saved')
  assert.equal((await api.restoreV4Final(access,{restore_id:'mock-restore',expected_store_version:2,expected_plan_revision:2,expected_save_id:'mock-save'})).envelope.plan_revision,2)
  assert.equal(requests.filter(r=>r.path==='/api/trip/v4/tasks'&&r.method==='post').length,1);assert.equal(requests.find(r=>r.path.endsWith('/edits')).body.command.operations[0].type,'remove_activity');assert.ok(requests.filter(r=>r.path!=='/api/trip/v4/tasks').every(r=>r.authorized));assert.ok(requests.every(r=>!r.path.includes(access.accessToken)));assert.equal(networkAttempts,0)
 }finally{axios.create=originalCreate;http.request=originalHttp;https.request=originalHttps;globalThis.fetch=originalFetch}
})
test('实际页面交付动作保留交通、缓存编辑和保存入口，退出后不采用',async()=>{
 const page=await read('../src/views/TripPreview.vue'),action=page.slice(page.indexOf('async function loadUntilReady('),page.indexOf('async function loadFinalState('))
 const code=compile(`export function create(deps){const {waitForV4PlanningPreview,getV4TaskStatus,getV4Preview,mapV4Preview,generationStage,generationStatus,view,envelope,activeAccess,activeIndex,pageState,loadFinalState,hasStoredV4PendingEdit,readV4PendingEdit,pendingEditCacheBroken,pendingEdit,message,startRouteJobForEnvelope,sendPendingEdit,wait}=deps;let disposed=false,requestGeneration=1;${action}return{loadUntilReady,stop:()=>{disposed=true;requestGeneration++}}}`),create=(await import(url(code))).create
 for(const cached of [null,{planId:access.planId,status:'pending'},{planId:access.planId,status:'conflict'}]){const calls=[],state=Object.fromEntries(['generationStage','generationStatus','view','envelope','activeAccess','activeIndex','pageState','pendingEditCacheBroken','pendingEdit'].map(k=>[k,{value:null}]))
  const instance=create({...state,waitForV4PlanningPreview:session.waitForV4PlanningPreview,getV4TaskStatus:async()=>status('ready','ready'),getV4Preview:async()=>snapshot(),mapV4Preview:value=>({city:value.preview.planning_input.request.city}),loadFinalState:()=>calls.push('final'),hasStoredV4PendingEdit:()=>!!cached,readV4PendingEdit:()=>cached,message:{info:()=>{}},startRouteJobForEnvelope:()=>calls.push('routes'),sendPendingEdit:()=>calls.push('edit'),wait:async()=>{}})
  await instance.loadUntilReady(access,1);assert.equal(state.pageState.value,'ready');assert.equal(state.envelope.value.task_id,access.taskId);assert.deepEqual(calls,['final',cached?.status==='pending'?'edit':'routes']);instance.stop();state.pageState.value='stopped';await instance.loadUntilReady(access,1);assert.equal(state.pageState.value,'stopped')
 }
 assert.ok(page.includes("pageState !== 'loading'"));assert.ok(page.includes('planningObservationTarget(activeAccess.value?.taskId)'));const plan=await read('../src/views/TripPlan.vue');assert.ok(plan.includes('query: { task: access.taskId }'))
})

test('实际预览页面渲染加载、失败、交通等待/失败与编辑保存入口',async()=>{
 const page=await read('../src/views/TripPreview.vue')
 for(const state of ['loading','failed','pending','route_failed']) {
  let initialization=`\npageState.value=${JSON.stringify(state==='loading'||state==='failed'?state:'ready')};activeAccess.value=${JSON.stringify(access)};generationStage.value='repair';generationStatus.value='running';errorMessage.value='模拟规划失败';failureAction.value='replan';\n`
  if(['pending','route_failed'].includes(state))initialization+=`\nenvelope.value=${JSON.stringify(snapshot())};view.value=mapV4Preview(envelope.value);routeJobState.value=${JSON.stringify(state==='pending'?'pending':'failed')};\n`
  const {descriptor}=parse(page.replace('</script>',initialization+'\n</script>'))
  let compiled=compile(compileScript(descriptor,{id:'native-preview-page',inlineTemplate:true}).content)
  const ast=ts.createSourceFile('component.js',compiled,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS)
  const replacements=new Map([['vue',import.meta.resolve('vue')],['@/utils/v4PlanningSession',sessionURL],['@/utils/v4PreviewProgress',progressURL]])
  for(const node of ast.statements.filter(ts.isImportDeclaration)) {
   const name=node.moduleSpecifier.text
   if(replacements.has(name))continue
   let moduleSource
   if(name==='vue-router')moduleSource=`export const useRoute=()=>({query:{task:'${access.taskId}'}})`
   else if(name==='ant-design-vue')moduleSource='export const message={info:()=>{},error:()=>{},success:()=>{}}'
   else if(name==='@/services/v4Preview') {
    const names=node.importClause.namedBindings.elements.map(item=>item.propertyName?.text??item.name.text)
    moduleSource=names.map(n=>`export const ${n}=()=>{throw new Error('SSR不应访问服务:${n}')}`).join(';')
   } else if(name==='@/components/preview/PlanningProgress.vue') {
    const {descriptor:d}=parse(await read('../src/components/preview/PlanningProgress.vue'))
    moduleSource=compile(compileScript(d,{id:'actual-native-progress',inlineTemplate:true}).content)
    for(const [n,target] of replacements)moduleSource=moduleSource.replaceAll(`from '${n}'`,`from '${target}'`).replaceAll(`from "${n}"`,`from "${target}"`)
   } else if(name.endsWith('.vue'))moduleSource=`import {h} from '${import.meta.resolve('vue')}';export default {render(){return h('div',{'data-mock-map':'true'},'模拟地图')}}`
   else if(name.startsWith('@/utils/'))moduleSource=compile(await read('../src/'+name.slice(2)+'.ts'))
   else throw new Error('未模拟的页面依赖:'+name)
   replacements.set(name,url(moduleSource))
  }
  for(const [name,target] of replacements)compiled=compiled.replaceAll(`from '${name}'`,`from '${target}'`).replaceAll(`from "${name}"`,`from "${target}"`)
  const component=(await import(url(compiled))).default,router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{template:'<div />'}},{path:'/observability',component:{template:'<div />'}}]})
  await router.push('/');await router.isReady();const app=createSSRApp(component);app.use(router);const html=await renderToString(app)
  assert.ok(html.includes(`/observability?task=${access.taskId}&amp;view=trace`),state+html.slice(0,1000));assert.ok(!html.includes(access.accessToken))
  if(state==='loading')assert.ok(html.includes('由主智能体修订'))
  else if(state==='failed')assert.ok(html.includes('重新规划'))
  else {assert.ok(html.includes('导出全部日期'));assert.ok(html.includes('还原上次保存'));assert.ok(html.includes('删除活动：'));assert.ok(html.includes('模拟地图'));assert.ok(html.includes('继续编辑'))}
 }
})
