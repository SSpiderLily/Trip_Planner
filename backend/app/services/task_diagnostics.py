"""只读汇总一次任务的诊断证据。旧日志未记录的事实不能由报告补猜。"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from app.services.observation_service import sanitize
from app.services.relational_storage import StorageDatabase


def _object(value):
    try:
        decoded = json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _select(source, keys):
    return {key: source[key] for key in keys if key in source}


def _details(output):
    result = _select(output, (
        "schema_version", "stage", "outcome", "valid", "accepted", "error_code", "exception_type",
        "issue_count", "diagnostics_truncated", "collection_stop_reason", "stop_reason",
        "preserved_activity_count", "modifiable_activity_count", "removable_activity_count",
        "targets_with_new_usable_candidates", "remaining_collection_targets",
        "status", "target_count", "recovered_count", "unresolved_count", "finish_reason",
    ))
    if isinstance(output.get("model_call"), dict):
        model = output["model_call"]
        result["model_call"] = _select(model, (
            "outcome", "finish_reason", "exception_chain", "http_status", "provider_code", "request_id",
        ))
        if isinstance(model.get("usage_counts"), dict):
            result["model_call"]["usage_counts"] = _select(model["usage_counts"], ("input", "output", "total", "reasoning"))
    result["diagnostics"] = []
    diagnostics = output.get("diagnostics")
    for item in (diagnostics[:8] if isinstance(diagnostics, list) else []):
        if not isinstance(item, dict):
            continue
        detail = _select(item, ("code", "path", "guidance", "reason_codes", "eligible_candidate_count", "outcome"))
        if isinstance(item.get("time_comparison"), dict):
            detail["time_comparison"] = _select(item["time_comparison"], (
                "leg_departure_at", "route_fact_departure_at", "same_query_minute", "difference_s",
            ))
        result["diagnostics"].append(detail)
    return result


@contextmanager
def _read_connection(source):
    database = StorageDatabase(source, 'observation')
    if database.is_postgres:
        with closing(database.connect(autocommit=True)) as connection:
            # 同一只读快照；不初始化表、不恢复任务、不获取仓库写锁。
            connection.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            try:
                yield connection, 'span_sequence'
            finally:
                connection.rollback()
    else:
        with closing(sqlite3.connect(database.path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            yield connection, 'rowid'


def build_task_report(database: Path | str, task_id: str) -> dict:
    """不加载项目配置，不初始化仓库，不恢复/清理任务，也不调用模型或地图。"""
    with _read_connection(database) as (connection, span_order):
        task = connection.execute(
            "SELECT task_id,status,started_at,finished_at,error_code,error_step,observation_incomplete "
            "FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if task is None:
            raise LookupError("TASK_NOT_FOUND")
        rows = [dict(row) for row in connection.execute(
            "SELECT span_id,parent_span_id,name,operation_type,status,started_at,finished_at,input_data,output_data,error,"
            f"input_truncated,output_truncated FROM spans WHERE task_id=? ORDER BY started_at,{span_order}", (task_id,),
        )]

    by_id = {row["span_id"]: row for row in rows}
    events, rounds, failures = [], [], []
    delivery = None
    direct_signals = []
    for row in rows:
        output, error = _object(row["output_data"]), _object(row["error"])
        metadata = _object(row["input_data"])
        # 子调用继承轮次关联，读取时沿已有父链查找；不会用时间相邻猜轮次。
        cursor, visited = row, set()
        association = {}
        while cursor and cursor["span_id"] not in visited:
            visited.add(cursor["span_id"])
            for key, value in _select(_object(cursor["input_data"]), (
                "request_id", "plan_id", "day_id", "round_id", "round_number", "base_revision", "proposed_revision",
            )).items():
                association.setdefault(key, value)
            cursor = by_id.get(cursor["parent_span_id"])
        model = output.get("model_call") if isinstance(output.get("model_call"), dict) else {}
        finish = model.get("finish_reason") or output.get("finish_reason")
        # 只数直接调用/校验事件，排除agent、pipeline和轮次对同一错误的包装。
        signal = None
        if row["operation_type"] == "llm":
            if row["status"] == "failed" or model.get("outcome") == "error":
                signal = ("model_interface", output.get("code") or error.get("code") or "MODEL_CALL_FAILED")
            elif finish and finish != "stop":
                signal = ("model_completion", finish)
            elif output.get("status") == "invalid":
                signal = ("validation", output.get("code") or "OUTPUT_INVALID")
        elif row["operation_type"] == "validation" and output.get("valid") is False:
            code = output.get("error_code") or output.get("reason") or "OUTPUT_INVALID"
            signal = ("model_completion" if code.endswith("OUTPUT_INCOMPLETE") or code == "truncated" else "validation", code)
        if signal:
            direct_signals.append((row["span_id"], row["name"], *signal))
        details = _details(output)
        rejected = (row["status"] in {"failed", "interrupted"} or output.get("valid") is False
                    or bool(output.get("error_code")) or output.get("outcome") in {"rejected", "cancelled"})
        if row["name"] == "planning.v4.run" and output.get("outcome") in {"draft_ready", "draft_ready_with_gaps"}:
            delivery = _select(output, ("outcome", "revision", "issue_counts", "days"))
        selected = (rejected or row["name"].startswith("repair.")
                    or row["name"] in {"planning.v4.run", "planning.v4.progress", "planning.dispatch",
                                       "validation.daily_plan_local_repair"})
        if not selected and not model and not finish:
            continue
        event = {
            "span_id": row["span_id"], "parent_span_id": row["parent_span_id"],
            "name": row["name"], "execution_status": row["status"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            **association, **details,
        }
        if row["name"] == "planning.v4.progress":
            event["stage"] = metadata.get("stage")
        if not event.get("error_code") and error.get("code"):
            event["error_code"] = error["code"]
        events.append(event)
        if row["name"] == "repair.round_result":
            rounds.append(event)
        if rejected:
            failures.append(event)

    # repair阶段也使用validation类型；沿父链剔除同一故障的传播记录。
    suppressed = set()
    signals_by_id = {item[0]: item for item in direct_signals}
    for sid, name, category, code in direct_signals:
        if name == "validation.daily_plan_completion":
            prior = [r for r in rows if r["parent_span_id"] == by_id[sid]["parent_span_id"]
                     and r["operation_type"] == "llm" and r["finished_at"]
                     and r["finished_at"] <= by_id[sid]["started_at"]]
            latest = max(prior, key=lambda r: r["finished_at"]) if prior else None
            sibling = signals_by_id.get(latest["span_id"]) if latest else None
            if sibling and sibling[2] == "model_completion":
                suppressed.add(sid)
        cursor, visited = by_id.get(by_id[sid]["parent_span_id"]), set()
        while cursor and cursor["span_id"] not in visited:
            parent_id = cursor["span_id"]
            visited.add(parent_id)
            parent = signals_by_id.get(parent_id)
            if parent and (parent[3] == code or
                           category == "model_completion" and parent[3].endswith("OUTPUT_INCOMPLETE")):
                suppressed.add(parent_id)
            cursor = by_id.get(cursor["parent_span_id"])
    distribution = Counter((name, category, code) for sid, name, category, code in direct_signals
                           if sid not in suppressed)

    gaps = []
    if task["observation_incomplete"]:
        gaps.append("采集器已标记记录不完整，部分阶段可能未保存。")
    if any(row["input_truncated"] or row["output_truncated"] for row in rows):
        gaps.append("部分记录被截断，不能据此认定没有其他错误。")
    if task["status"] in {"accepted", "running"}:
        gaps.append("任务尚未结束，此报告只是读取时的快照。")
    if not delivery:
        gaps.append("没有最终交付质量记录，不能由任务状态推断每日安排完整。")
    if not rounds and (delivery is None or any(day.get("repair_rounds", 0) for day in delivery.get("days", []))):
        gaps.append("没有逐轮采用记录；若发生过修订，无法确认各轮是否采用及拒绝原因。")
    if any(not event.get("diagnostics") for event in failures):
        gaps.append("部分拒绝只有错误码，没有具体条件；报告不会猜测缺失原因。")
    return sanitize({
        "schema_version": 1,
        "task": dict(task),
        "span_count": len(rows),
        "error_distribution": [
            {"stage": name, "category": category, "code": code, "count": count}
            for (name, category, code), count in sorted(distribution.items(), key=lambda item: (-item[1], item[0]))
        ],
        # 父调用开始更早，但可能在子调用失败后才失败；按错误记录完成时刻选首条。
        "first_observed_rejection": min(failures, key=lambda event: event["finished_at"] or event["started_at"]) if failures else None,
        "delivery": delivery,
        "repair_rounds": rounds,
        "events": events,
        "evidence_gaps": gaps,
        "evidence_boundary": "第一条已记录异常不等于最终根因；报告只读，不执行或重跑任务，也不包含完整请求或模型回答。",
    })


def render_task_report(report: dict) -> str:
    task = report["task"]
    lines = [f"任务 {task['task_id']}｜执行状态 {task['status']}｜{report['span_count']} 条记录"]
    if task.get("error_code"):
        lines.append(f"任务错误：{task['error_code']} @ {task.get('error_step') or '未知阶段'}")
    first = report["first_observed_rejection"]
    if first:
        lines.append(f"首条已记录异常：{first['name']} / {first.get('error_code', 'UNKNOWN')}")
    else:
        lines.append("首条已记录异常：未发现（不代表无错误）")
    delivery = report["delivery"]
    lines.append(f"初版生成结果：{delivery['outcome'] if delivery else '未知'}")
    if delivery:
        for day in delivery.get("days", []):
            lines.append(f"  {day['day_id']}：景点 {day['sightseeing_count']}，用餐 {day['meal_count']}，"
                         f"自由活动 {day['free_time_count']}，缺失时段 {day['unfilled_slot_count']}，"
                         f"完整性 {day['completeness']}，补查结束 {day['repair_stop_reason']}")
    lines.append("直接失败/校验分布（不计父级包装；调用异常与输出校验分别计数）：")
    for item in report.get("error_distribution", []):
        lines.append(f"  {item['stage']} / {item['category']} / {item['code']}：{item['count']} 次")
    lines.append("阶段与逐轮决策（按开始时间）：")
    for event in report["events"]:
        association = f" {event.get('day_id', '')} 第{event['round_number']}轮" if event.get("round_number") else ""
        label = f"阶段 {event.get('stage')}" if event["name"] == "planning.v4.progress" else event["name"]
        lines.append(f"  {event['started_at']} {label}{association}："
                     f"调用={event['execution_status']}，结果={event.get('outcome', '未单独记录')} {event.get('error_code') or ''}"
                     f" [{event['span_id']}]")
        if event.get("model_call"):
            lines.append("    模型接口：" + json.dumps(event["model_call"], ensure_ascii=False))
        elif event.get("finish_reason"):
            lines.append("    结束原因：" + event["finish_reason"])
        for detail in event.get("diagnostics", []):
            codes = detail.get("code") or ",".join(detail.get("reason_codes", [])) or "未记录"
            lines.append(f"    {codes} @ {detail.get('path')}")
            if detail.get("guidance"):
                lines.append("    条件：" + detail["guidance"])
            if "eligible_candidate_count" in detail:
                lines.append(f"    可用候选 {detail['eligible_candidate_count']}；处理结果 {detail.get('outcome', '未记录')}")
            if detail.get("time_comparison"):
                lines.append("    " + json.dumps(detail["time_comparison"], ensure_ascii=False))
        if "targets_with_new_usable_candidates" in event:
            lines.append(f"    新增可用候选覆盖 {event['targets_with_new_usable_candidates']} 个目标；"
                         f"仍需补查 {event['remaining_collection_targets']} 个目标")
    lines.extend("证据缺口：" + gap for gap in report["evidence_gaps"])
    lines.append(report["evidence_boundary"])
    return "\n".join(lines)
