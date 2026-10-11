export type V4TaskState = 'accepted' | 'running' | 'ready' | 'failed' | 'interrupted' | string

/** 只根据后端状态和阶段映射进度，不推算百分比或剩余时间。 */
export function v4PreviewStageLabel(stage: string, status: V4TaskState): string {
  if (status === 'accepted') return '已收到需求，正在准备规划'

  const labels: Record<string, string> = {
    accepted: '已收到需求，正在准备规划',
    planning_input: '正在整理旅行需求',
    dispatch: '正在查询景点、餐馆、住宿和天气',
    proposal: '正在规划每天的行程',
    current_routes: '正在计算交通路线和活动时间',
    timing: '正在核对游览时间与返程安排',
    validation: '正在检查行程安排和缺项',
    repair: '正在检查并补齐每天的行程',
    repair_collection: '正在补查行程中的缺项',
    repair_revision: '正在调整每天的行程',
    repair_routes: '正在重新计算当天交通和时间',
    cost: '正在汇总已核实的消费参考',
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
    default:
      if (errorCode === 'PIPELINE_DEADLINE' || errorCode?.includes('TIMEOUT')) {
        return '规划过程超过可用时间，未能生成预览。请返回首页调整需求后重新规划。'
      }
      if (errorCode?.startsWith('DISPATCH_')) {
        return '地点或天气信息未能完成核实，因此无法生成预览。请返回首页重新规划。'
      }
      if (errorCode?.startsWith('CURRENT_ROUTE_') || errorCode?.startsWith('ROUTE_')) {
        return '交通与活动时间未能完成核算，因此无法生成预览。请返回首页重新规划。'
      }
      return '规划过程未能完成，因此没有可预览的行程。请返回首页重新规划。'
  }
}

/** 网络或服务端读取失败可重读；身份和其他客户端错误应返回首页。 */
export function canRetryV4PreviewRead(hasResponse: boolean, status: number | null): boolean {
  if (!hasResponse) return true
  return status === 429 || (status !== null && status >= 500)
}
