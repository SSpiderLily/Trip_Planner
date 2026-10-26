import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/services/v4Preview.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const axiosUrl = new URL('../node_modules/axios/index.js', import.meta.url).href
// Load the real mapper dependency introduced by destination selection; Node does
// not resolve Vite's @ alias from an in-memory TypeScript module.
const mapperSource = await readFile(new URL('../src/utils/v4DestinationSearchMapper.ts', import.meta.url), 'utf8')
const mapperCompiled = ts.transpileModule(mapperSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const mapperUrl = `data:text/javascript;base64,${Buffer.from(mapperCompiled).toString('base64')}`
const runnable = compiled
  .replace("from 'axios'", `from '${axiosUrl}'`)
  .replace("from '@/utils/v4DestinationSearchMapper'", `from '${mapperUrl}'`)
  .replace("const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'", "const API_BASE_URL = 'http://localhost:8000'")
const service = await import(`data:text/javascript;base64,${Buffer.from(runnable).toString('base64')}`)

const memory = new Map()
let rejectWrites = false
globalThis.localStorage = {
  getItem: key => memory.get(key) ?? null,
  setItem: (key, value) => {
    if (rejectWrites) throw new Error('storage blocked')
    memory.set(key, value)
  },
  removeItem: key => memory.delete(key),
}

service.savePreviewAccess('task-first', 'synthetic-token-1', 'plan-first')
assert.equal(service.getPendingPreviewAccess(), null)
assert.deepEqual(service.getLatestPreviewAccess(), {
  taskId: 'task-first', planId: 'plan-first', accessToken: 'synthetic-token-1',
})

rejectWrites = true
assert.throws(() => service.savePreviewAccess('task-second', 'synthetic-token-2', 'plan-second'), /当前页面仍可继续查看/)
assert.deepEqual(service.getPendingPreviewAccess(), {
  taskId: 'task-second', planId: 'plan-second', accessToken: 'synthetic-token-2',
})
assert.deepEqual(service.getLatestPreviewAccess(), service.getPendingPreviewAccess(),
  'preview navigation after a blocked write must keep the latest task in memory, not fall back to an older task')
assert.equal(service.retrySavePreviewAccess(), false)

rejectWrites = false
assert.equal(service.retrySavePreviewAccess(), true, 'retry only persists the existing access and never resubmits planning')
assert.equal(service.getPendingPreviewAccess(), null)
assert.deepEqual(service.getLatestPreviewAccess(), {
  taskId: 'task-second', planId: 'plan-second', accessToken: 'synthetic-token-2',
})

console.log('v4PreviewAccessStorage: in-memory continuation and persistence-only retry passed')

assert.equal(service.getPreviewAccessForTask('task-first')?.taskId, 'task-first',
  'generating another task must retain the earlier task credential')
assert.equal(service.getPreviewAccessForTask('task-missing'), null,
  'an explicit missing task must never fall back to the latest task')
rejectWrites = true
assert.throws(() => service.savePreviewAccess('task-pending', 'synthetic-token-3', 'plan-pending'))
assert.equal(service.getPreviewAccessForTask('task-pending')?.taskId, 'task-pending')
assert.equal(service.getPreviewAccessForTask('task-first')?.taskId, 'task-first',
  'a pending newer task must not replace the explicitly selected task')
console.log('v4PreviewAccessStorage: explicit task selection and retained credentials passed')

assert.equal(service.v4PreviewErrorMessage({
  isAxiosError: true,
  response: { status: 409, data: { detail: { code: 'PREVIEW_CONTRACT_UNSUPPORTED' } } },
}), '这份行程数据已损坏或不支持当前版本，请返回首页重新创建行程。')

const unsupported = {
  isAxiosError: true,
  response: { status: 409, data: { detail: { code: 'PREVIEW_CONTRACT_UNSUPPORTED' } } },
}
assert.equal(service.isV4EditConflict(unsupported), false,
  'unsupported records must not enter version-conflict rebase and retry')
assert.match(service.v4EditErrorMessage(unsupported), /已损坏或不支持当前版本/)
assert.match(service.v4DestinationSelectErrorMessage(unsupported), /已损坏或不支持当前版本/)
assert.match(service.v4FinalErrorMessage(unsupported, 'restore'), /无法还原.*当前行程内容保持不变/)
assert.match(service.v4FinalErrorMessage(unsupported, 'save'), /保存未完成.*图片仍可使用/)
assert.equal(service.isV4EditConflict({
  isAxiosError: true, response: { status: 409, data: { detail: { code: 'PREVIEW_VERSION_CONFLICT' } } },
}), true)

// 观测使用原任务凭据及独立客户端，不把令牌放入URL。
assert.equal(typeof service.getV4ObservationMetrics, 'function')
assert.equal(typeof service.getV4ObservationSpans, 'function')
assert.equal(typeof service.getV4ObservationSpan, 'function')
