import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/services/v4Preview.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const axiosUrl = new URL('../node_modules/axios/index.js', import.meta.url).href
const runnable = compiled
  .replace("from 'axios'", `from '${axiosUrl}'`)
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
