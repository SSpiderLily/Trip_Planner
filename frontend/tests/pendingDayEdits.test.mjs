import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/services/pendingDayEdits.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 }
}).outputText
const { activityOrderMoves, compactCancelledPendingAdds } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

const add = { operation: { type: 'add_poi', poi_id: 'poi-1', client_activity_id: 'new-1' } }
const moveAdded = { operation: { type: 'move_activity', activity_id: 'new-1', direction: 'up' } }
const deleteAdded = { operation: { type: 'delete_activity', activity_id: 'new-1', confirmed: true } }
const keepOther = { operation: { type: 'move_activity', activity_id: 'B', direction: 'up' } }
const keepMode = { operation: { type: 'select_leg_mode', from_activity_id: 'A', to_activity_id: 'B', mode: 'walking' } }

const applyMoves = (initial, edits) => edits.reduce((order, { operation }) => {
  if (operation.type === 'add_poi') order.push(operation.client_activity_id)
  if (operation.type === 'delete_activity') order.splice(order.indexOf(operation.activity_id), 1)
  if (operation.type === 'move_activity') {
    const index = order.indexOf(operation.activity_id)
    const target = index + (operation.direction === 'up' ? -1 : 1)
    if (index >= 0 && target >= 0 && target < order.length) [order[index], order[target]] = [order[target], order[index]]
  }
  return order
}, [...initial])

const cancelled = compactCancelledPendingAdds([add, moveAdded, deleteAdded], ['A', 'B'])
assert.deepEqual(cancelled.edits, [], 'add→move→delete should collapse')
assert.deepEqual(cancelled.cancelledActivityIds, ['new-1'])

const reorderAfterCancel = compactCancelledPendingAdds([
  add,
  moveAdded,
  { operation: { type: 'move_activity', activity_id: 'B', direction: 'up' } },
  deleteAdded
], ['A', 'B'])
assert.deepEqual(reorderAfterCancel.edits.map(edit => edit.operation), [
  { type: 'move_activity', activity_id: 'B', direction: 'up' },
  { type: 'move_activity', activity_id: 'A', direction: 'up' }
], 'the retained B move needs a correction to preserve the final visible order')
assert.deepEqual(applyMoves(['A', 'B'], reorderAfterCancel.edits), ['A', 'B'])

const unrelated = compactCancelledPendingAdds([add, moveAdded, deleteAdded, keepOther, keepMode], ['A', 'B'])
assert.deepEqual(unrelated.edits.map(edit => edit.operation), [keepOther.operation, keepMode.operation], 'unrelated activity and leg edits should remain queued')
assert.deepEqual(compactCancelledPendingAdds([deleteAdded], ['A', 'B', 'new-1']).edits, [deleteAdded], 'an in-flight add must not be cancelled before its response')
assert.deepEqual(activityOrderMoves(['B', 'A'], ['A', 'B']), [{ type: 'move_activity', activity_id: 'A', direction: 'up' }])
console.log('pendingDayEdits: 7 assertions passed')
