import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import { mixedSyntheticEnvelope } from './v4PreviewFixtures.mjs'

async function importTypeScript(path, names) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const loaded = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
  return Object.fromEntries(names.map(name => [name, loaded[name]]))
}

const { mapV4Preview } = await importTypeScript('../src/utils/v4PreviewMapper.ts', ['mapV4Preview'])
const { captureV4PreviewExport, exportV4Snapshot, canApplyV4Restore } = await importTypeScript(
  '../src/utils/v4PreviewExportController.ts',
  ['captureV4PreviewExport', 'exportV4Snapshot', 'canApplyV4Restore'],
)

const sourceEnvelope = mixedSyntheticEnvelope()
const sourceView = mapV4Preview(sourceEnvelope)
const snapshot = captureV4PreviewExport(sourceEnvelope, sourceView, { dirty: false, saving: false })
assert.equal(snapshot.persistence.storeVersion, sourceEnvelope.store_version)
assert.equal(snapshot.persistence.planRevision, sourceEnvelope.plan_revision)
assert.ok(Object.isFrozen(snapshot.visible) && Object.isFrozen(snapshot.visible.envelope.preview.draft.days[0].activities))
sourceEnvelope.preview.draft.days[0].theme = '后续变更'
assert.notEqual(snapshot.visible.envelope.preview.draft.days[0].theme, '后续变更', 'an export keeps the view captured before asynchronous rendering')

for (const pageState of [{ dirty: true, saving: false }, { dirty: false, saving: true }]) {
  assert.equal(captureV4PreviewExport(sourceEnvelope, sourceView, pageState).persistence, null,
    'dirty or in-flight content still exports but cannot save an older server snapshot')
}

const events = []
let saveCalls = 0
const ordered = await exportV4Snapshot({
  snapshot,
  renderAll: async visible => {
    events.push('render')
    assert.equal(visible.view.days.length, sourceView.days.length)
    return [{ filename: 'day-1.png', blob: 'one' }, { filename: 'day-2.png', blob: 'two' }]
  },
  startDownloads: files => {
    events.push(`download:${files.length}`)
  },
  saveFinal: async source => {
    events.push('save')
    saveCalls++
    assert.equal(source.storeVersion, sourceEnvelope.store_version)
    return { status: 'saved' }
  },
  isSaved: response => response.status === 'saved',
})
assert.deepEqual(events, ['render', 'download:2', 'save'], 'all images start downloading before one best-effort save')
assert.equal(saveCalls, 1)
assert.equal(ordered.status, 'saved')

const skippedEvents = []
const dirtySnapshot = captureV4PreviewExport(sourceEnvelope, sourceView, { dirty: true, saving: false })
const skipped = await exportV4Snapshot({
  snapshot: dirtySnapshot,
  renderAll: async () => [{ filename: 'day-1.png', blob: 'one' }],
  startDownloads: files => skippedEvents.push(`download:${files.length}`),
  saveFinal: async () => {
    skippedEvents.push('save')
    throw new Error('must not submit stale content')
  },
  isSaved: () => true,
})
assert.equal(skipped.status, 'skipped')
assert.deepEqual(skippedEvents, ['download:1'])

let becameDirty = false
let lateSaveCalled = false
const dirtyDuringRender = await exportV4Snapshot({
  snapshot,
  renderAll: async () => {
    becameDirty = true
    return [{ filename: 'day-1.png', blob: 'one' }]
  },
  startDownloads: () => {},
  canPersist: () => !becameDirty,
  saveFinal: async () => { lateSaveCalled = true },
  isSaved: () => true,
})
assert.equal(dirtyDuringRender.status, 'skipped', 'new edits during image rendering also suppress a stale server save')
assert.equal(lateSaveCalled, false)

const saveFailureEvents = []
const failedSave = await exportV4Snapshot({
  snapshot,
  renderAll: async () => [{ filename: 'day-1.png', blob: 'one' }],
  startDownloads: () => saveFailureEvents.push('download'),
  saveFinal: async () => {
    saveFailureEvents.push('save')
    throw new Error('503')
  },
  isSaved: () => true,
})
assert.equal(failedSave.status, 'failed')
assert.deepEqual(saveFailureEvents, ['download', 'save'], 'a save outage happens after the download starts')

for (const routeState of ['pending', 'failed']) {
  const placesFirst = mixedSyntheticEnvelope()
  placesFirst.preview.calculation.current_routes = routeState
  const placesView = mapV4Preview(placesFirst)
  const captured = captureV4PreviewExport(placesFirst, placesView, { dirty: false, saving: false })
  assert.ok(captured.persistence, `${routeState} traffic does not block saving a place snapshot`)
  const events = []
  const result = await exportV4Snapshot({
    snapshot: captured,
    renderAll: async visible => {
      // A later traffic response cannot change the image currently being rendered.
      placesFirst.preview.calculation.current_routes = 'completed'
      placesFirst.store_version += 1
      assert.equal(visible.envelope.preview.calculation.current_routes, routeState)
      assert.equal(visible.view.days.length, placesView.days.length)
      events.push('render')
      return [{ filename: 'places-first.png', blob: 'synthetic-image' }]
    },
    startDownloads: () => events.push('download'),
    saveFinal: async source => {
      assert.equal(source.storeVersion, captured.visible.envelope.store_version)
      events.push('save')
      throw new Error('storage unavailable')
    },
    isSaved: () => true,
  })
  assert.equal(result.status, 'failed')
  assert.deepEqual(events, ['render', 'download', 'save'])
}

let didDownload = false
let didSave = false
await assert.rejects(exportV4Snapshot({
  snapshot,
  renderAll: async () => { throw new Error('image render failed') },
  startDownloads: () => { didDownload = true },
  saveFinal: async () => { didSave = true },
  isSaved: () => true,
}), /image render failed/)
assert.equal(didDownload, false)
assert.equal(didSave, false, 'render failure starts no download and does not save')

const stale = await exportV4Snapshot({
  snapshot,
  renderAll: async () => [{ filename: 'day-1.png', blob: 'one' }],
  startDownloads: () => {},
  saveFinal: async () => ({ status: 'stale' }),
  isSaved: response => response.status === 'saved',
})
assert.equal(stale.status, 'stale', 'CAS stale is reported after export without undoing it')

const expected = {
  task_id: 'task-1', plan_id: 'plan-1', store_version: 7, plan_revision: 3,
}
assert.equal(canApplyV4Restore({ dirty: false, saving: false, current: expected, expected }), true)
assert.equal(canApplyV4Restore({ dirty: true, saving: false, current: expected, expected }), false)
assert.equal(canApplyV4Restore({ dirty: false, saving: true, current: expected, expected }), false)
assert.equal(canApplyV4Restore({
  dirty: false, saving: false, current: { ...expected, store_version: 8 }, expected,
}), false, 'late restore response cannot overwrite a changed page version')

const exportComponent = await readFile(new URL('../src/views/TripPreviewExport.vue', import.meta.url), 'utf8')
const previewPage = await readFile(new URL('../src/views/TripPreview.vue', import.meta.url), 'utf8')
assert.match(exportComponent, /v-for="\(day, index\) in visible\.view\.days"/, 'every date is rendered')
assert.match(exportComponent, /v4-export-missing/, 'missing and conditional slots are exported')
assert.match(exportComponent, /v4-export-route/, 'route references are exported')
assert.match(exportComponent, /v4-export-global-issues/, 'global issues are exported')
assert.match(exportComponent, /MAX_PAGE_HEIGHT/, 'long dates are split into browser-safe pages')
assert.match(exportComponent, /splitOversizedBlock/, 'oversized text blocks receive their own continuation pages')
assert.match(exportComponent, /scale: 2/, 'pagination preserves readable scale instead of shrinking a whole day')
assert.doesNotMatch(exportComponent, /recommendations|候选地点/, 'unplanned candidates are excluded from export')
assert.match(exportComponent, /全程旅费预算 ¥\{\{ visible\.view\.tripBudget \}\}/)
assert.doesNotMatch(exportComponent, /全程旅费预算 ¥\{\{ visible\.view\.tripBudget \}\} \/ 人/)
assert.match(exportComponent, /已查询到的消费小计 \/ 人/)
assert.match(previewPage, /await nextTick\(\)[\s\S]{0,180}const renderer = exportRenderer\.value/, 'the renderer is mounted before its method is read')
assert.match(previewPage, /exportFiles\.length[\s\S]*?href="file\.url"/, 'generated files remain available for individual downloads')
assert.match(previewPage, /URL\.createObjectURL\(file\.blob\)/, 'download fallback retains each generated image')

console.log('v4PreviewExportController: frozen export, download/save ordering, stale/offline save, and restore guards passed')
