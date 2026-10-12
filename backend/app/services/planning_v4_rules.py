"""生产 v4 规划的确定性时间规则。"""
from __future__ import annotations

from app.models.planning_v4.input import ReferenceWindow, RuleSet


PLANNING_V4_RULE_VERSION = "planning-v4-2026-10-12"

_WINDOWS = (
    ("breakfast", 8 * 60, 9 * 60),
    ("morning", 9 * 60, 12 * 60),
    ("lunch", 12 * 60, 14 * 60),
    ("afternoon", 14 * 60, 17 * 60),
    ("dinner", 17 * 60, 19 * 60),
    ("evening", 19 * 60, 22 * 60),
)


def default_planning_v4_rules() -> RuleSet:
    """返回六个窗口；时段转换把1/3小时作为资格阈值。"""
    return RuleSet(
        rule_version=PLANNING_V4_RULE_VERSION,
        windows=tuple(
            ReferenceWindow(period=period, start_minute=start, end_minute=end)
            for period, start, end in _WINDOWS
        ),
        meal_minimum_available_s=3600,
        sightseeing_minimum_available_s=10800,
        max_repair_rounds_per_day=3,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
        arrival_meals_use_actual_availability=True,
        short_arrival_window_policy="skip_without_missing",
        departure_day_activities_below_minimum="skip_without_missing",
        return_buffer_priority="explicit_user_value_then_default",
    )
