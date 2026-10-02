"""对每一天按活动顺序推算参考时间；未知路段不会被当作零分钟。"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def _local_datetime(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=TZ)


def schedule_day(day: dict, conditions: dict) -> dict:
    """设置活动时刻与可能超时摘要；输入对象会被原地更新并返回。"""
    day_date = day["date"]
    arrival_value, departure_value = conditions.get("arrival_at"), conditions.get("departure_at")
    arrival = _as_local(arrival_value) if _on_date(arrival_value, day_date) else None
    departure = _as_local(departure_value) if _on_date(departure_value, day_date) else None
    buffer_minutes = (30 if arrival else 0) + (30 if departure else 0)
    window_start = max(_local_datetime(day_date, 9), arrival if arrival else _local_datetime(day_date, 9))
    window_end = min(_local_datetime(day_date, 19), departure if departure else _local_datetime(day_date, 19))
    start = max(_local_datetime(day_date, 9), arrival + timedelta(minutes=30) if arrival else window_start)
    deadline = min(_local_datetime(day_date, 19), departure - timedelta(minutes=30) if departure else window_end)

    legs = day.get("legs", [])
    leg_by_target = {leg.get("to_activity_id"): leg for leg in legs}
    cursor: datetime | None = start
    known_minutes = buffer_minutes
    unknown_legs = 0
    unknown_activities = 0
    for activity in day.get("activities", []):
        leg = leg_by_target.get(activity.get("activity_id"))
        if leg is not None:
            duration = _selected_duration(leg)
            if duration is None:
                unknown_legs += 1
                cursor = None
            else:
                known_minutes += duration
                if cursor is not None:
                    cursor += timedelta(minutes=duration)
        duration = activity.get("duration_minutes")
        if duration is None:
            unknown_activities += 1
            activity["start_at"] = None
            activity["end_at"] = None
            cursor = None
        else:
            known_minutes += duration
            if cursor is not None:
                activity["start_at"] = cursor.isoformat(timespec="minutes")
                cursor += timedelta(minutes=duration)
                activity["end_at"] = cursor.isoformat(timespec="minutes")
            else:
                activity["start_at"] = None
                activity["end_at"] = None
    last_leg = next((leg for leg in legs if leg.get("to_activity_id") in ("lodging", "departure")), None)
    if last_leg is not None:
        duration = _selected_duration(last_leg)
        if duration is None:
            unknown_legs += 1
            cursor = None
        else:
            known_minutes += duration
            if cursor is not None:
                cursor += timedelta(minutes=duration)

    available_minutes = max(0, int((window_end - window_start).total_seconds() // 60))
    overrun = max(0, known_minutes - available_minutes)
    status = "possible_overrun" if overrun else ("incomplete" if unknown_legs or unknown_activities else "within_budget")
    if cursor is not None and cursor > deadline:
        overrun = max(overrun, int((cursor - deadline).total_seconds() // 60 + 0.999))
        status = "possible_overrun"
    day["time_summary"] = {
        "known_minutes": known_minutes,
        "known_total": known_minutes,
        "unknown_leg_count": unknown_legs,
        "unknown_activity_count": unknown_activities,
        "possible_overrun_minutes": overrun,
        "budget_minutes": available_minutes,
        "buffer_minutes": buffer_minutes,
        "complete": unknown_legs == 0 and unknown_activities == 0,
        "status": status,
    }
    return day


def _selected_duration(leg: dict) -> int | None:
    mode = leg.get("selected_mode") or leg.get("mode")
    options = leg.get("options") or {}
    option = options.get(mode, {}) if isinstance(options, dict) else {}
    if option:
        return option.get("duration_minutes") if option.get("status") == "available" else None
    # 兼容仅持有 legacy 路段的只读结果。
    return leg.get("duration_minutes")


def _as_local(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def _on_date(value: str | None, day: str) -> bool:
    return bool(value) and _as_local(value).date().isoformat() == day


def opening_conflict(activity: dict) -> bool | None:
    """只判断可明确解析的单日开放时段；复杂时段返回None，不臆断。"""
    opening = str(activity.get('opening_hours') or '')
    start, end = activity.get('start_at'), activity.get('end_at')
    if not opening or not start or not end:
        return None
    intervals = re.findall(r'(?<!\d)(\d{1,2}:\d{2})\s*[-–—~至]\s*(\d{1,2}:\d{2})(?!\d)', opening)
    if not intervals:
        return None
    try:
        def minutes(value):
            hour, minute = (int(part) for part in value.split(':'))
            return hour * 60 + minute
        starts_at, ends_at = minutes(start[11:16]), minutes(end[11:16])
        windows = [(minutes(opens), minutes(closes)) for opens, closes in intervals]
        windows = [(opens, closes) for opens, closes in windows if opens <= closes]
        if not windows:
            return None
        return not any(starts_at >= opens and ends_at <= closes for opens, closes in windows)
    except (TypeError, ValueError, IndexError):
        return None
