import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const helperSource = await readFile(new URL('../src/utils/v4PreviewProgress.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(helperSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const progress = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

assert.equal(progress.v4PreviewStageLabel('planning_input', 'accepted'), '已收到需求，正在准备规划')
assert.equal(progress.v4PreviewStageLabel('dispatch', 'running'), '正在查询景点、餐馆、住宿和天气')
assert.equal(progress.v4PreviewStageLabel('proposal', 'running'), '正在规划每天的行程')
assert.equal(progress.v4PreviewStageLabel('current_routes', 'running'), '正在计算交通路线和活动时间')
assert.equal(progress.v4PreviewStageLabel('timing', 'running'), '正在核对游览时间与返程安排')
assert.equal(progress.v4PreviewStageLabel('repair_revision', 'running'), '正在调整每天的行程')
assert.equal(progress.v4PreviewStageLabel('cost', 'running'), '正在汇总已核实的消费参考')
assert.equal(progress.v4PreviewStageLabel('unexpected_stage', 'running'), '正在准备旅行规划')

const invalidOutput = progress.v4GenerationFailureMessage('PLAN_OUTPUT_INVALID')
assert.equal(invalidOutput, '规划结果格式不完整，未能生成可用行程。请返回首页重新规划。')
assert.doesNotMatch(invalidOutput, /PLAN_OUTPUT_INVALID|模型|JSON|字段/)
assert.match(progress.v4GenerationFailureMessage('PLAN_GENERATION_TIMEOUT'), /超过可用时间/)
assert.match(progress.v4GenerationFailureMessage('DISPATCH_RESULT_INVALID'), /地点或天气信息/)
assert.match(progress.v4GenerationFailureMessage('unknown-code'), /规划过程未能完成/)
assert.doesNotMatch(progress.v4GenerationFailureMessage('unknown-code'), /unknown-code/)

assert.equal(progress.canRetryV4PreviewRead(false, null), true, 'a network error can offer a safe GET retry')
assert.equal(progress.canRetryV4PreviewRead(true, 503), true)
assert.equal(progress.canRetryV4PreviewRead(true, 429), true)
assert.equal(progress.canRetryV4PreviewRead(true, 404), false, 'invalid access returns the user to planning')
assert.equal(progress.canRetryV4PreviewRead(true, 422), false)

console.log('v4 preview progress and failure handling checks passed')
