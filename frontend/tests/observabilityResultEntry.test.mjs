import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// Run the actual component action with controlled API/router/storage boundaries.
const source = await readFile(new URL('../src/views/Observability.vue', import.meta.url), 'utf8')
const action = source.slice(source.indexOf('async function showResult()'), source.indexOf('watch(() => [route.query.task'))
const compiled = ts.transpileModule(`export function create(deps) {
  const { selectedId, resultError, getTaskResult, getPreviewAccessForTask, recoverLocalPreviewAccess, router, sessionStorage } = deps
  const scoped = false
  let disposed = false, taskVersion = 1
  const errorText = () => 'synthetic query error'
  ${action}
  return { showResult, select: id => { selectedId.value = id; taskVersion++ } }
}`, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const { create } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
function fixture(api, access = () => ({ taskId: 'selected' }), recover = async () => {}) {
  const paths = [], writes = [], error = { value: '' }
  const state = create({ selectedId: { value: 'selected' }, resultError: error,
    getTaskResult: api, getPreviewAccessForTask: access, recoverLocalPreviewAccess: recover,
    router: { push: async target => paths.push(target) },
    sessionStorage: { setItem: (...args) => writes.push(args) } })
  return { ...state, paths, writes, error }
}
const v4Error = { response: { status: 409, data: { detail: { code: 'PREVIEW_RESULT_USE_PREVIEW_API' } } } }
const v4 = fixture(async () => { throw v4Error })
await v4.showResult()
assert.deepEqual(v4.paths, [{ path: '/preview', query: { task: 'selected' } }])
assert.equal(v4.writes.length, 0)
const missing = fixture(async () => { throw v4Error }, () => null)
await missing.showResult()
assert.deepEqual(missing.paths, [{ path: '/preview', query: { task: 'selected' } }])
const recoveryFailure = fixture(async () => { throw v4Error }, () => null, async () => { throw new Error('recover failed') })
await recoveryFailure.showResult()
assert.equal(recoveryFailure.paths.length, 0)
assert.equal(recoveryFailure.error.value, 'synthetic query error')
const legacy = fixture(async () => ({ data: { city: 'synthetic' } }))
await legacy.showResult()
assert.deepEqual(legacy.paths, ['/result'])
assert.equal(legacy.writes[1][1], 'selected')
const otherFailure = fixture(async () => { throw new Error('network') })
await otherFailure.showResult()
assert.equal(otherFailure.paths.length, 0)
assert.equal(otherFailure.error.value, 'synthetic query error')
let reject
const stale = fixture(() => new Promise((_, fail) => { reject = fail }))
const loading = stale.showResult()
stale.select('another-task')
reject(v4Error)
await loading
assert.equal(stale.paths.length, 0)
assert.equal(stale.error.value, '')
console.log('observabilityResultEntry: v4, legacy, missing credentials, network errors and stale responses passed')
