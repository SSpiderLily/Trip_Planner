import type { SpanSummary, TaskStatus } from '@/types'

export const isActive = (task: Pick<TaskStatus, 'status'>) => task.status === 'accepted' || task.status === 'running'
export const isCall = (span: SpanSummary) => span.operation_type === 'llm' || span.operation_type === 'tool'
export const time = (value: string | null | undefined): number | null => {
  const result = value ? Date.parse(value) : NaN
  return Number.isFinite(result) ? result : null
}
export function durationMs(span: Pick<SpanSummary, 'started_at' | 'finished_at'>): number | null {
  const start = time(span.started_at), end = time(span.finished_at)
  return start !== null && end !== null && end >= start ? end - start : null
}
export function timingIssue(span: Pick<SpanSummary, 'started_at' | 'finished_at'>): string {
  if (time(span.started_at) === null || (span.finished_at !== null && time(span.finished_at) === null)) return '起止时间无效'
  if (span.finished_at !== null && time(span.finished_at)! < time(span.started_at)!) return '结束时间早于开始时间'
  return ''
}
export const numberOrNull = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
export function normalizeSpan<T extends SpanSummary>(span: T): T {
  return { ...span, input_tokens: numberOrNull(span.input_tokens), output_tokens: numberOrNull(span.output_tokens), total_tokens: numberOrNull(span.total_tokens) }
}
export const tokenTotal = (span: SpanSummary) => span.total_tokens ?? (span.input_tokens !== null && span.output_tokens !== null ? span.input_tokens + span.output_tokens : null)
export const tokenText = (value: number | null) => value === null ? '未采集' : value.toLocaleString()
export const milliseconds = (value: number | null) => value === null ? '耗时未知' : value < 1000 ? `${Math.round(value * 10) / 10} 毫秒` : `${(value / 1000).toFixed(1)} 秒`
export function spanState(span: SpanSummary, active: boolean) {
  if (span.status === 'running') return active ? '执行中' : '状态未确定'
  return { succeeded: '成功', failed: isCall(span) ? '调用失败' : '失败', interrupted: '中断未完成' }[span.status] || '状态未确定'
}
export function spanTime(span: SpanSummary, active: boolean, now: number) {
  const duration = durationMs(span), start = time(span.started_at)
  if (span.status === 'running' && active && start !== null && now >= start) return `已执行 ${milliseconds(now - start)}`
  return milliseconds(duration)
}
export interface TraceNode { span: SpanSummary; parent: string | null; children: string[]; warning: string }
export interface TraceForest { nodes: Map<string, TraceNode>; roots: string[]; ordered: SpanSummary[] }
export function buildForest(spans: SpanSummary[]): TraceForest {
  const ordered = [...spans].sort((a, b) => (time(a.started_at) ?? Infinity) - (time(b.started_at) ?? Infinity) || a.span_id.localeCompare(b.span_id))
  const nodes = new Map<string, TraceNode>()
  for (const span of ordered) nodes.set(span.span_id, { span, parent: span.parent_span_id, children: [], warning: '' })
  for (const node of nodes.values()) {
    if (node.parent && !nodes.has(node.parent)) { node.parent = null; node.warning = '父级记录缺失' }
  }
  for (const id of nodes.keys()) {
    const visited = new Set<string>()
    let current: string | null = id
    while (current) {
      const node: TraceNode = nodes.get(current)!
      if (visited.has(current)) { node.parent = null; node.warning = '父级循环，已断开'; break }
      visited.add(current); current = node.parent
    }
  }
  const roots: string[] = []
  for (const [id, node] of nodes) {
    if (node.parent) nodes.get(node.parent)!.children.push(id)
    else roots.push(id)
  }
  return { nodes, roots, ordered }
}
export function ancestors(forest: TraceForest, id: string) {
  const ids: string[] = [], visited = new Set<string>()
  let parent = forest.nodes.get(id)?.parent
  while (parent && !visited.has(parent)) { visited.add(parent); ids.push(parent); parent = forest.nodes.get(parent)?.parent }
  return ids.reverse()
}
export type TraceFilter = 'all' | 'model' | 'errors' | 'slow'
export function traceRows(forest: TraceForest, expanded: Set<string>, filter: TraceFilter, query: string, label: (name: string) => string) {
  const search = query.trim().toLocaleLowerCase(), filtering = filter !== 'all' || !!search
  const hits = new Set(forest.ordered.filter(s => {
    const kind = filter === 'all' || (filter === 'model' && s.operation_type === 'llm') || (filter === 'errors' && isCall(s) && ['failed', 'interrupted'].includes(s.status)) || (filter === 'slow' && isCall(s) && (durationMs(s) ?? -1) >= 10000)
    return kind && (!search || `${label(s.name)} ${s.name}`.toLocaleLowerCase().includes(search))
  }).map(s => s.span_id))
  const keep = new Set(hits)
  if (filtering) for (const id of hits) for (const parent of ancestors(forest, id)) keep.add(parent)
  const rows: { node: TraceNode; depth: number; context: boolean }[] = []
  const stack = forest.roots.map(id => ({ id, depth: 0 })).reverse()
  while (stack.length) {
    const { id, depth } = stack.pop()!, node = forest.nodes.get(id)!
    if (filtering && !keep.has(id)) continue
    rows.push({ node, depth, context: filtering && !hits.has(id) })
    if (filtering || expanded.has(id)) for (const child of [...node.children].reverse()) stack.push({ id: child, depth: depth + 1 })
  }
  return rows
}
export function defaultExpanded(forest: TraceForest) {
  return forest.ordered.filter(s => forest.nodes.get(s.span_id)?.parent === null || s.operation_type === 'agent' || forest.nodes.get(s.span_id)?.children.some(id => forest.nodes.get(id)?.span.operation_type === 'llm')).map(s => s.span_id)
}
export function defaultSelection(spans: SpanSummary[]) {
  for (const kind of ['llm', 'tool']) {
    const rows = spans.filter(s => s.operation_type === kind && durationMs(s) !== null).sort((a, b) => durationMs(b)! - durationMs(a)!)
    if (rows[0]) return rows[0].span_id
  }
  return buildForest(spans).roots[0] ?? ''
}
export function timelineBounds(task: TaskStatus, spans: SpanSummary[], now: number) {
  const starts = spans.map(s => time(s.started_at)).filter((x): x is number => x !== null)
  const origin = time(task.started_at) ?? (starts.length ? Math.min(...starts) : time(task.created_at) ?? now)
  const points = spans.flatMap(s => durationMs(s) !== null ? [time(s.started_at)!, time(s.finished_at)!] : time(s.started_at) !== null ? [time(s.started_at)!] : [])
  const left = Math.min(origin, ...points), right = Math.max(origin, ...points, time(task.finished_at) ?? origin, isActive(task) ? now : origin)
  return { origin, left, right, length: Math.max(1, right - left) }
}
export function anomalyCounts(spans: SpanSummary[]) {
  const own = spans.filter(isCall), failed = own.filter(s => s.status === 'failed').length, interrupted = own.filter(s => s.status === 'interrupted').length
  return { failed, interrupted, total: failed + interrupted }
}
export function toolRows(spans: SpanSummary[]) {
  const groups = new Map<string, SpanSummary[]>()
  for (const span of spans.filter(s => s.operation_type === 'tool')) groups.set(span.name, [...(groups.get(span.name) ?? []), span])
  return [...groups].map(([name, calls]) => {
    const known = calls.map(durationMs).filter((x): x is number => x !== null)
    const succeeded = calls.filter(s => s.status === 'succeeded').length, failed = calls.filter(s => s.status === 'failed').length
    return { name, calls, total: calls.length, succeeded, failed, interrupted: calls.filter(s => s.status === 'interrupted').length, unknown: calls.filter(s => s.status === 'running').length,
      duration: known.length ? known.reduce((a, b) => a + b, 0) : null, average: known.length ? known.reduce((a, b) => a + b, 0) / known.length : null, timed: known.length,
      successRate: succeeded + failed ? succeeded / (succeeded + failed) : null }
  })
}
