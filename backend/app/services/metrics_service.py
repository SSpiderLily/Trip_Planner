"""单任务工程指标；只使用任务摘要和调用元数据。"""
from datetime import datetime


ROLE_NAMES = {
    'collect_sights': '候选搜集', 'collect_support': '候选搜集',
    'arrange_layout': '行程安排', 'arrange_final': '行程安排',
    'repair_layout': '行程安排', 'repair_final': '行程安排',
    'revision': '行程修改',
    'attraction': '景点搜索', 'weather': '天气查询',
    'hotel': '酒店搜索', 'planner': '行程整合',
}


def elapsed_ms(start, end):
    if not start or not end:
        return None
    try:
        return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))
    except (ValueError, TypeError):
        return None


def usage_total(row):
    if row['total_tokens'] is not None:
        return row['total_tokens']
    if row['input_tokens'] is not None and row['output_tokens'] is not None:
        return row['input_tokens'] + row['output_tokens']
    return None


def token_summary(rows):
    def known(field):
        values = [row[field] for row in rows if row[field] is not None]
        return sum(values) if values else None
    totals = [usage_total(row) for row in rows]
    covered = sum(row['input_tokens'] is not None and row['output_tokens'] is not None and total is not None
                  for row, total in zip(rows, totals))
    available_totals = [value for value in totals if value is not None]
    return {
        'input_tokens': known('input_tokens'),
        'output_tokens': known('output_tokens'),
        'total_tokens': sum(available_totals) if available_totals else None,
        'covered_calls': covered,
        'total_calls': len(rows),
        'complete': covered == len(rows),
    }


def call_summary(rows):
    counts = {status: sum(row['status'] == status for row in rows)
              for status in ('succeeded', 'failed', 'interrupted', 'running')}
    known = [duration for row in rows if (duration := elapsed_ms(row['started_at'], row['finished_at'])) is not None]
    denominator = counts['succeeded'] + counts['failed']
    return {
        'total': len(rows), 'succeeded': counts['succeeded'], 'failed': counts['failed'],
        'interrupted': counts['interrupted'], 'unknown': counts['running'],
        'success_rate': counts['succeeded'] / denominator if denominator else None,
        'duration_ms': sum(known) if known else None,
        'duration_complete': len(known) == len(rows),
    }


def role_for(row, by_id):
    parent = row['parent_span_id']
    visited = set()
    while parent and parent not in visited:
        visited.add(parent)
        ancestor = by_id.get(parent)
        if ancestor is None:
            break
        if ancestor['operation_type'] == 'agent':
            name = ancestor['name'].removeprefix('agent.')
            return ROLE_NAMES.get(name, ancestor['name'])
        parent = ancestor['parent_span_id']
    return '未归属'


def build_metrics(task, spans):
    by_id = {row['span_id']: row for row in spans}
    models = [row for row in spans if row['operation_type'] == 'llm']
    tools = [row for row in spans if row['operation_type'] == 'tool']
    roles, tool_names = {}, {}
    for row in models:
        roles.setdefault(role_for(row, by_id), []).append(row)
    for row in tools:
        tool_names.setdefault(row['name'].removeprefix('tool.'), []).append(row)
    return {
        'task': {
            'status': task['status'],
            'duration_ms': elapsed_ms(task.get('created_at'), task.get('finished_at')),
            'observation_incomplete': task.get('observation_incomplete', False),
            'error_code': task.get('error_code'), 'error_message': task.get('error_message'),
            'error_step': task.get('error_step'), 'persistence_error': task.get('persistence_error'),
        },
        'model': call_summary(models), 'tool': call_summary(tools),
        'tokens': token_summary(models),
        'model_details': [
            {'name': name, **call_summary(rows), 'tokens': token_summary(rows)}
            for name, rows in roles.items()
        ],
        'tool_details': [
            {'name': name, **call_summary(rows)} for name, rows in tool_names.items()
        ],
    }
