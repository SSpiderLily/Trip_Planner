"""纯离线评估：重算确定性指标，不推断语义质量、耗时或模型用量。"""
from hashlib import sha256
import json
from app.models.planning_errors import InvalidPlanningReferences
from app.models.planning_execution import PlanningSnapshot, DraftResult
from app.models.planning_evaluation import (
    QualityReview, QualityRatings, PlanningEvaluation, DeterministicMetrics,
)
from app.services.planning_v4_validation import validate_daily_plan, is_required_place_gap
from app.services.planning_v4_validator import ContractValidationError
from app.services.planning_lodging_policy import lodging_stays, choose_lodging_for_stay
from .validation import validate_snapshot, validate_selected_draft


def evaluate_draft(snapshot: PlanningSnapshot, result: DraftResult, *,
                   review: QualityReview | None = None) -> PlanningEvaluation:
    snapshot = PlanningSnapshot.model_validate(snapshot.model_dump(mode='python'))
    result = DraftResult.model_validate(result.model_dump(mode='python'))
    validate_snapshot(snapshot)
    draft = result.draft
    # 同一身份也可能携带另一批资料；整个输入边界必须一致。
    if (result.identity != snapshot.identity
            or result.planning_input != snapshot.planning_input
            or result.facts != snapshot.facts or result.candidates != snapshot.candidates
            or result.area_hints != snapshot.area_hints
            or draft.plan_id != snapshot.identity.plan_id
            or draft.source_run_id != snapshot.identity.task_id
            or draft.request_id != snapshot.planning_input.request.request_id
            or draft.rule_version != snapshot.planning_input.rules.rule_version
            or result.report.plan_id != draft.plan_id
            or result.report.based_on_revision != draft.revision):
        raise ValueError('EVALUATION_IDENTITY_MISMATCH')
    # 固定案例多次生成沿用同一身份/版本；人工结论还须绑定实际草稿及其依据。
    fingerprint = sha256(json.dumps(result.model_dump(mode='json'), sort_keys=True,
                                   ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    if review is not None:
        review = QualityReview.model_validate(review.model_dump(mode='python'))
        if (review.identity != result.identity or review.request_id != draft.request_id
                or review.rule_version != draft.rule_version or review.revision != draft.revision
                or review.result_fingerprint != fingerprint):
            raise ValueError('EVALUATION_REVIEW_MISMATCH')

    references_valid = True
    validated_report = None
    try:
        validated_report = validate_daily_plan(snapshot.planning_input, draft, snapshot.facts, snapshot.candidates)
        validate_selected_draft(snapshot, draft)
    except (ContractValidationError, InvalidPlanningReferences):
        references_valid = False
    if draft.area_hints != snapshot.area_hints:
        references_valid = False
    required = {slot.slot_id for day in snapshot.planning_input.days for slot in day.slots
                if slot.eligibility == 'required'}
    # 引用无效时不把地点计为已核实覆盖；不是重新接受或修订草稿。
    missing = {issue.target_id for issue in validated_report.issues
               if is_required_place_gap(snapshot.planning_input, issue)} if validated_report else required
    covered = required - missing if references_valid else set()
    days = {day.day_id: day for day in draft.days}
    lodging_bound = True
    for stay in lodging_stays(snapshot.planning_input):
        candidate = choose_lodging_for_stay(snapshot.planning_input, stay, snapshot.candidates,
                                           snapshot.facts, snapshot.area_hints)
        for day_id in stay.day_ids:
            lodging = days.get(day_id).lodging if day_id in days else None
            if (candidate is None or lodging is None
                    or lodging.candidate_ref != candidate.candidate_id
                    or lodging.poi_id != candidate.poi_id
                    or lodging.place_fact_id != candidate.place_fact_id
                    or lodging.area_hint_id != candidate.area_hint_id
                    or lodging.selection_source != ('user' if stay.specified else 'recommended')):
                lodging_bound = False
    return PlanningEvaluation(
        identity=result.identity, request_id=draft.request_id, rule_version=draft.rule_version,
        revision=draft.revision, result_fingerprint=fingerprint,
        deterministic=DeterministicMetrics(delivered=True, references_valid=references_valid,
            required_slots=len(required), covered_required_slots=len(covered),
            required_coverage_complete=references_valid and covered == required,
            lodging_bound=lodging_bound),
        human=review.ratings if review is not None else QualityRatings(),
    )
