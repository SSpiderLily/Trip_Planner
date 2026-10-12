"""区分候选的采集覆盖范围与排程限制，不推断营业时间。"""
from app.models.planning_v4 import CandidateRef, PlanningInput


def candidate_allows_slot(candidate: CandidateRef, slot_id: str) -> bool:
    """只检查候选范围；日期、类型、skipped 和来源由调用方另行校验。

    景点 slot_ids 是搜索覆盖标签，不是已验证开放时间。餐馆候选的
    slot_ids 同时关联该餐次搜索区域，暂保留其约束，避免跨区域误用。
    """
    return candidate.category == "sightseeing" or not candidate.slot_ids or slot_id in candidate.slot_ids


def applicable_slot_ids(candidate: CandidateRef, planning_input: PlanningInput) -> tuple[str, ...]:
    periods = {
        "sightseeing": {"morning", "afternoon", "evening"},
        "meal": {"breakfast", "lunch", "dinner"},
    }.get(candidate.category, set())
    return tuple(
        slot.slot_id for day in planning_input.days for slot in day.slots
        if slot.eligibility != "skipped" and slot.period in periods
        and candidate_allows_slot(candidate, slot.slot_id)
    )
