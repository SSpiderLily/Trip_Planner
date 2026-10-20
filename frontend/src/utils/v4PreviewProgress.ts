export type V4TaskState = 'accepted' | 'running' | 'ready' | 'failed' | 'interrupted' | string

/** 只根据后端状态和阶段映射进度，不推算百分比或剩余时间。 */
export function v4PreviewStageLabel(stage: string, status: V4TaskState): string {
  if (status === 'accepted') return '已收到需求，正在准备规划'
  if (status === 'failed') return '行程未能生成'
  if (status === 'interrupted') return '行程规划已中断'
  if (status === 'ready') return '地点行程已生成，正在读取预览'

  const labels: Record<string, string> = {
    accepted: '已收到需求，正在准备规划',
    planning_input: '主智能体正在整理旅行需求',
    dispatch: '主智能体正在协调景点、餐饮和住宿查询，并核实旅行事实',
    proposal: '主智能体正在根据候选地点规划每日行程',
    current_routes: '正在查询交通路线',
    validation: '正在核对每日安排、地点依据和必需时段',
    repair: '正在核对缺项，按需补查并由主智能体修订对应日期',
    repair_collection: '正在补查行程中的缺项',
    repair_revision: '正在调整每天的行程',
    repair_routes: '正在核实当天交通',
    cost: '正在汇总消费参考并准备交付预览',
    ready: '行程已生成，正在打开预览',
    failed: '行程未能生成',
    interrupted: '行程规划已中断',
  }
  return labels[stage] ?? '正在准备旅行规划'
}

/** 对终态失败码使用用户可读的安全说明，不显示后端或模型原始错误。 */
export function v4GenerationFailureMessage(errorCode: string | null): string {
  switch (errorCode) {
    case 'PLAN_OUTPUT_INVALID':
      return '规划结果格式不完整，未能生成可用行程。请返回首页重新规划。'
    case 'PLAN_OUTPUT_INCOMPLETE':
    case 'PLAN_OUTPUT_TRUNCATED':
    case 'PLAN_DRAFT_MISSING':
      return '行程安排没有完整生成，因此无法打开预览。请返回首页重新规划。'
    case 'TRIP_REQUEST_INVALID':
    case 'REQUIREMENT_INTERPRETATION_FAILED':
    case 'REQUIREMENT_SUMMARY_INVALID':
      return '旅行需求未能整理完成，因此无法生成预览。请返回首页重新规划。'
    case 'MASTER_DID_NOT_SUBMIT_REQUIREMENTS':
    case 'REQUIREMENT_SOURCE_INVALID':
    case 'REQUIREMENT_PREFERENCES_INCOMPLETE':
      return '旅行需求未能整理完成，因此无法生成预览。请返回首页重新规划。'
    case 'MODEL_CALL_FAILED':
      return '规划服务本次未能完成响应，因此没有可预览的行程。请稍后返回首页重新规划。'
    case 'MASTER_DID_NOT_SUBMIT_PLAN':
    case 'NATIVE_PLANNING_TECHNICAL_ERROR':
      return '每日行程未能完成生成或修订，因此无法交付预览。请返回首页重新规划。'
    case 'NATIVE_PREVIEW_INVALID':
    case 'NATIVE_RESULT_IDENTITY_MISMATCH':
    case 'NATIVE_REPORT_VERSION_MISMATCH':
    case 'NATIVE_FACTS_MISSING':
      return '行程结果未通过交付核对，因此无法打开预览。请返回首页重新规划。'
    default:
      if (errorCode === 'PIPELINE_DEADLINE' || errorCode?.includes('TIMEOUT')) {
        return '规划过程超过可用时间，未能生成预览。请返回首页调整需求后重新规划。'
      }
      if (errorCode?.startsWith('DISPATCH_')) {
        return '地点或天气信息未能完成核实，因此无法生成预览。请返回首页重新规划。'
      }
      if (errorCode?.startsWith('CURRENT_ROUTE_') || errorCode?.startsWith('ROUTE_')) {
        return '交通路线未能完成查询，因此无法生成预览。请返回首页重新规划。'
      }
      return '规划过程未能完成，因此没有可预览的行程。请返回首页重新规划。'
  }
}

/** 网络或服务端读取失败可重读；身份和其他客户端错误应返回首页。 */
export function canRetryV4PreviewRead(hasResponse: boolean, status: number | null): boolean {
  if (!hasResponse) return true
  return status === 429 || (status !== null && status >= 500)
}
