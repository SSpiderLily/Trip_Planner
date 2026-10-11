import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/v4DestinationSearchPagination.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText
const pagination = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const response = (overrides = {}) => ({
  page: 1,
  status: 'ok',
  provider_count: null,
  candidates: [],
  omitted_count: 0,
  ...overrides,
})

assert.equal(pagination.canQueryNextDestinationPage(response({ candidates: [{}] })), true,
  'with an unknown raw count, visible results allow the user to query the next page')
assert.equal(pagination.canQueryNextDestinationPage(response({ omitted_count: 3 })), true,
  'screened-out raw results still allow querying the next page')
assert.equal(pagination.canQueryNextDestinationPage(response()), false,
  'an empty first page with no omitted results has no next page to query')
assert.equal(pagination.canQueryNextDestinationPage(response({ provider_count: 41, page: 2 })), true)
assert.equal(pagination.canQueryNextDestinationPage(response({ provider_count: 40, page: 2 })), false)
assert.equal(pagination.canQueryNextDestinationPage(response({ status: 'error', page: 2, provider_count: null, candidates: [{}] })), false)
assert.equal(pagination.shouldShowDestinationSearchPagination(response({ page: 2, status: 'empty' })), true,
  'an empty later page keeps a visible previous-page route')
assert.equal(pagination.shouldShowDestinationSearchPagination(response({ page: 2, status: 'error' })), true,
  'a failed later page also keeps the previous-page route')
assert.equal(pagination.shouldShowDestinationSearchPagination(response()), false)

console.log('v4DestinationSearchPagination: unknown-count continuation and empty-page back navigation passed')
