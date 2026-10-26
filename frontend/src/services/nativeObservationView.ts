import type { SpanDetail } from '@/types'

export const nativeStepNames: Record<string, string> = {
  'planning.modular': '独立分层规划', 'planning.modular.requirements': '需求整理阶段',
  'planning.modular.collection': '资料采集阶段', 'planning.modular.generation': '初版生成阶段',
  'planning.modular.validation': '行程提交校验', 'planning.modular.evaluation': '独立评估阶段',
  'llm.native_tool_loop': '原生模型调用', 'tool.native_call': '原生工具调用',
  'native.tool.arguments_rejected': '工具参数未通过',
  'native.agent.attractions': '景点子智能体执行', 'native.agent.dining': '餐饮子智能体执行',
  'native.agent.lodging': '住宿子智能体执行',
  'native.requirements.start': '主智能体开始整理需求', 'native.requirements.accepted': '完整需求与原文依据',
  'native.requirements.submission_rejected': '需求提交未通过',
  'native.dispatch.round_start': '开始一轮任务派发', 'native.dispatch.round_end': '本轮任务合并',
  'native.master.task': '主智能体派发子任务', 'native.master.task_rejected': '子任务参数未通过',
  'native.map.request': '子智能体发起高德查询', 'native.map.result': '高德查询事实返回',
  'native.child.selection': '子智能体提交候选', 'native.child.selection_rejected': '候选提交未通过',
  'native.child.result': '子智能体返回候选与缺项', 'native.master.result': '主智能体汇总候选',
  'native.planning.phase': '主智能体切换规划阶段', 'native.planning.initial': '每日行程初版与校验',
  'native.planning.submission_rejected': '每日提案未通过',
  'native.repair.collect': '单日缺项补查任务', 'native.repair.collected': '单日补查结果',
  'native.repair.submission_rejected': '单日修订未通过', 'native.repair.revision': '主智能体提交单日修订',
  'native.repair.validation': '单日修订校验结果', 'native.planning.result': '最终每日行程与修订记录',
  'native.preview.converted': '原生结果转为预览', 'native.preview.route_context_unavailable': '交通上下文初始化失败',
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const roles: Record<string, string> = { master: '主智能体', attractions: '景点子智能体', dining: '餐饮子智能体', lodging: '住宿子智能体' }
const stages: Record<string, string> = { requirements: '需求整理', collection: '地点采集', daily_plan: '每日规划', revision: '单日修订' }

/** 摘要只帮助定位；完整已保存业务参数和结果仍由现有 JSON 详情展示。 */
export function nativeObservationSummary(detail: SpanDetail | null): string[] {
  if (!detail || !(detail.name in nativeStepNames)) return []
  const input = object(detail.input_data), output = object(detail.output_data)
  const result: string[] = []
  const role = input.role ?? output.role, stage = input.stage ?? output.stage
  if (typeof role === 'string' && roles[role]) result.push(roles[role])
  if (typeof input.target_role === 'string' && roles[input.target_role]) result.push(`派发对象：${roles[input.target_role]}`)
  if (typeof stage === 'string' && stages[stage]) result.push(`阶段：${stages[stage]}`)
  if (typeof input.sequence === 'number') result.push(`业务记录序号：${input.sequence}`)
  const round = input.round ?? output.round
  if (typeof round === 'number') result.push(`派发或修订轮次：${round}`)
  const subtask = input.subtask_id ?? (detail.name.startsWith('native.map.') || detail.name.startsWith('native.child.') ? output.task_id : null)
  if (typeof subtask === 'string') result.push(`子任务：${subtask}`)
  const tool = output.tool_name ?? input.tool_name
  if (typeof tool === 'string') result.push(`工具：${tool}`)
  if (output.outcome === 'rejected') result.push('业务提交未通过；查看错误码与后续纠正记录')
  const candidates = object(output.task_result).candidates ?? output.candidates
  if (Array.isArray(candidates)) result.push(`返回候选：${candidates.length} 个`)
  if (Array.isArray(output.returned_places)) result.push(`地图返回地点：${output.returned_places.length} 个（是否采用查看候选记录）`)
  const draft = object(output.draft), revision = object(output.revision)
  const version = draft.revision ?? revision.revision
  if (typeof version === 'number') result.push(`行程版本：${version}`)
  if (Array.isArray(draft.days)) result.push(`每日安排：${draft.days.length} 天`)
  if (typeof output.stop_reason === 'string') result.push(`补查停止原因：${output.stop_reason}`)
  return result
}
