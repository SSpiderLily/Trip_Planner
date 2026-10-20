import type { V4DestinationSearchCandidate } from '@/types/v4Preview'

export interface V4DestinationSearchCandidateView {
  key: string
  name: string
  address: string
  city: string
  typeName: string
  costLabel: string
  openingHoursLabel: string
  sourceLabel: string
  queriedAtLabel: string
  missingFieldsLabel: string
  canSelect: boolean
  blockedReasonLabel: string
}

const BLOCK_REASON_LABELS: Record<NonNullable<V4DestinationSearchCandidate['selection_block_reason']>, string> = {
  location_unavailable: '没有可用位置，暂时不能加入',
  city_mismatch: '地点不在当前城市，暂时不能加入',
  category_unverified: '地点类别未核实，暂时不能加入',
  source_unclosed: '地点来源未能核实，暂时不能加入',
}

export function mapV4DestinationSearchCandidate(
  candidate: V4DestinationSearchCandidate,
  index: number,
): V4DestinationSearchCandidateView {
  const amount = candidate.cost.value
  const costLabel = amount !== null
    ? `人均参考消费：¥${amount}`
    : '未查询到可供参考的人均消费'
  const blockReason = candidate.selection_block_reason
  return {
    key: `${index}:${candidate.name}:${candidate.address ?? ''}`,
    name: candidate.name,
    address: candidate.address?.trim() || '地址未提供',
    city: candidate.city?.trim() || '',
    typeName: candidate.type_name?.trim() || '',
    costLabel,
    openingHoursLabel: candidate.opening_hours_raw?.trim()
      ? `营业时间：${candidate.opening_hours_raw.trim()}`
      : '营业时间未提供',
    sourceLabel: candidate.source.provider === 'amap' ? '高德地图' : '地点搜索来源',
    queriedAtLabel: formatDestinationSearchTime(candidate.source.queried_at),
    missingFieldsLabel: candidate.missing_fields.length ? '部分信息未提供' : '',
    canSelect: candidate.selectable && Boolean(candidate.candidate_token),
    blockedReasonLabel: blockReason ? BLOCK_REASON_LABELS[blockReason] : '',
  }
}

export function formatDestinationSearchTime(value: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '查询时间未提供'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date)
}

export function mapV4DestinationSelectFailure(input: {
  status: number | null
  code: string | null
  unavailable: boolean
}): string {
  if (input.code === 'DESTINATION_CANDIDATE_UNAVAILABLE') {
    return '搜索结果已失效，请重新搜索后再加入。原选择意图仍保留；可以重试核对，或停止重试并刷新。'
  }
  if (input.status === 422) {
    return '这项地点选择暂时无法应用。选择意图仍保留；请检查当前行程，或停止重试并刷新后再试。'
  }
  if (input.status === 404) return '无法访问这份行程；选择意图仍保留在本机。'
  if (input.status === 503 || (input.status !== null && input.status >= 500)) {
    return '尚未确认地点是否加入。选择意图仍保留；重试会使用同一命令核对结果，或停止重试并刷新。'
  }
  if (input.unavailable) {
    return '连接暂不可用，尚未确认地点是否加入。选择意图仍保留；重试会使用同一命令核对结果，或停止重试并刷新。'
  }
  return '尚未确认地点是否加入。选择意图仍保留；重试会使用同一命令核对结果，或停止重试并刷新服务端结果。'
}
