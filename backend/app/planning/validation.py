"""仅规划公共接受入口；规范化、校验、绑定事实，不调用外部服务。"""
from app.models.planning_execution import PlanningSnapshot, ProposalSubmission, DraftResult
from app.models.planning_v4 import PlanProposal, PlanDraft
from app.services.planning_v4_validation import validate_daily_plan
from app.services.planning_v4_validator import validate_plan_proposal
from .rules import DailyPlanRules, _reference_error
from .collection_evidence import validate_weather_evidence, validate_endpoint_evidence


def validate_snapshot(snapshot: PlanningSnapshot) -> None:
    note_ids = [note.task_id for note in snapshot.task_notes]
    if len(note_ids) != len(set(note_ids)) or not set(note_ids) <= {task.task_id for task in snapshot.task_results}:
        raise _reference_error(('collection_task_note_reference_invalid',))
    for evidence, validate, kinds in (
        (snapshot.weather_evidence, validate_weather_evidence, ('sources', 'weather')),
        (snapshot.endpoint_evidence, validate_endpoint_evidence, ('sources', 'places')),
    ):
        if evidence is None:
            continue
        validate(snapshot.planning_input, snapshot.identity.task_id, evidence)
        for kind in kinds:
            rows = {getattr(row, 'tool_call_id' if kind == 'sources' else 'fact_id'): row
                    for row in getattr(snapshot.facts, kind)}
            for row in getattr(evidence.fact_store, kind):
                key = getattr(row, 'tool_call_id' if kind == 'sources' else 'fact_id')
                if rows.get(key) != row:
                    raise _reference_error(('collection_evidence_fact_mismatch',))
    DailyPlanRules._validate_input_references(
        snapshot.planning_input, snapshot.candidates, snapshot.facts,
        snapshot.area_hints, snapshot.task_results, snapshot.identity.task_id,
    )


def validate_selected_draft(snapshot: PlanningSnapshot, draft: PlanDraft) -> None:
    """复用接受路径的已选地点检查；草稿与提案具有相同的日期/活动/住宿引用字段。"""
    DailyPlanRules()._validate_selected_candidates(
        draft, snapshot.planning_input, snapshot.candidates, snapshot.facts, snapshot.area_hints,
    )


def accept_submission(snapshot: PlanningSnapshot, submission: ProposalSubmission) -> DraftResult:
    # 重新解析边界值，拒绝通过 model_copy/model_construct 绕过契约的输入。
    snapshot = PlanningSnapshot.model_validate(snapshot.model_dump(mode='python'))
    submission = ProposalSubmission.model_validate(submission.model_dump(mode='python'))
    validate_snapshot(snapshot)
    proposal = DailyPlanRules()._validate_proposal_output(
        submission, snapshot.planning_input, snapshot.candidates, snapshot.facts, snapshot.area_hints,
    )
    return accept_proposal(snapshot, proposal)


def accept_proposal(snapshot: PlanningSnapshot, proposal: PlanProposal) -> DraftResult:
    snapshot = PlanningSnapshot.model_validate(snapshot.model_dump(mode='python'))
    proposal = PlanProposal.model_validate(proposal.model_dump(mode='python'))
    validate_snapshot(snapshot)
    if proposal.area_hints != snapshot.area_hints:
        raise _reference_error(('proposal_area_mismatch',))
    rules = DailyPlanRules()
    validate_plan_proposal(proposal, snapshot.planning_input, snapshot.candidates, snapshot.facts)
    rules._validate_selected_candidates(proposal, snapshot.planning_input, snapshot.candidates, snapshot.facts, snapshot.area_hints)
    draft = rules._build_draft(proposal, snapshot.planning_input, snapshot.candidates,
                              snapshot.facts, snapshot.area_hints, snapshot.identity.task_id,
                              plan_id=snapshot.identity.plan_id)
    report = validate_daily_plan(snapshot.planning_input, draft, snapshot.facts, snapshot.candidates)
    return DraftResult(identity=snapshot.identity, planning_input=snapshot.planning_input,
                       draft=draft, report=report, facts=snapshot.facts,
                       candidates=snapshot.candidates, area_hints=snapshot.area_hints)
