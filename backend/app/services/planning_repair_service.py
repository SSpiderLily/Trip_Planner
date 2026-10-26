"""P4装配：DayRepairService保持策略；模型和补查只经过独立值边界。"""
import asyncio
from app.models.planning_errors import PlanningError
from app.models.planning_v4 import DayRevision
from app.planning.context import build_revision_context
from app.planning.rules import DailyPlanRules
from app.services.day_repair_service import (
    DayRepairService, DayRepairError, DayRepairRecalculation, apply_day_revision,
)
from app.services.planning_v4_validation import validate_daily_plan


def accept_revision(request, submission):
    revision = DayRevision(day_id=request.day.day_id, base_revision=request.draft.revision,
        revision=request.draft.revision + 1, based_on_report_revision=request.report.based_on_revision,
        replacement_day=submission.replacement_day, preserve_activity_ids=request.preserve_activity_ids,
        source_issue_ids=tuple(i.issue_id for i in request.issues))
    DailyPlanRules._validate_revision_candidates(revision, request.planning_input, request.candidates,
        request.fact_store, request.area_hints, request.preserve_activity_ids, request.modifiable_activity_ids)
    apply_day_revision(revision, request.draft, request.report, request.planning_input,
        request.candidates, request.fact_store, expected_day_id=request.day.day_id,
        expected_source_issue_ids=revision.source_issue_ids, preserve_activity_ids=request.preserve_activity_ids,
        allowed_change_activity_ids=request.modifiable_activity_ids, removable_activity_ids=request.removable_activity_ids)
    return revision


async def repair_draft(snapshot, draft, report, *, revision_generator, collection, runtime, emit=None):
    """不向修订模型/DayRepairService暴露采集会话内部状态。"""
    from app.models.planning_execution import PlanningSnapshot
    from app.planning.validation import validate_snapshot
    snapshot = PlanningSnapshot.model_validate(snapshot.model_dump(mode='python'))
    validate_snapshot(snapshot)
    if (draft.plan_id != snapshot.identity.plan_id or draft.source_run_id != snapshot.identity.task_id
            or draft.request_id != snapshot.planning_input.request.request_id):
        raise PlanningError('REPAIR_IDENTITY_MISMATCH', '修订快照与草稿身份不一致')
    emit = emit or (lambda *args: None)
    async def collect(request):
        runtime.check_cancelled()
        runtime.report('repair', message=f'补查{request.day.day_date}的必需地点缺项')
        emit('repair.collect', {'day_id': request.day.day_id, 'round': request.round_number,
            'targets': [t.model_dump(mode='json') for t in request.targets]})
        try:
            result = await asyncio.to_thread(collection.supplement, request, runtime=runtime,
                cancel_event=getattr(runtime, 'cancel_event', None))
        except Exception as exc:
            runtime.check_cancelled()
            raise DayRepairError(getattr(exc, 'code', 'REPAIR_COLLECTION_FAILED'),
                fatal=getattr(exc, 'code', None) in {'CANCELLED', 'COLLECTION_IDENTITY_MISMATCH',
                    'REPAIR_REQUIREMENTS_CHANGED', 'COLLECTION_SESSION_MISMATCH'}) from None
        runtime.check_cancelled()
        from app.services.day_repair_service import _collection_scope
        constraint = next(d for d in request.planning_input.days if d.day_id == request.day.day_id)
        _, remaining = _collection_scope(request.planning_input, constraint, request.day,
            request.issues, result.fact_store, result.candidates)
        emit('repair.collected', {'day_id': request.day.day_id, 'stop_reason': result.stop_reason,
            'remaining_targets': len(remaining)})
        return result

    async def revise(request):
        runtime.check_cancelled()
        runtime.report('repair', message=f'修订{request.day.day_date}的安排')
        context = build_revision_context(request,
            task_notes=collection.task_notes if hasattr(type(collection), 'task_notes') else snapshot.task_notes,
            endpoint_evidence=snapshot.endpoint_evidence)
        accepted, issued = None, None
        def accept(submission):
            nonlocal accepted, issued
            runtime.check_cancelled()
            if accepted is not None:
                raise PlanningError('PLAN_ALREADY_ACCEPTED', '本次修订已接受')
            result = accept_revision(request, submission)
            runtime.check_cancelled()
            accepted, issued = result.model_copy(deep=True), result
            emit('repair.revision', {'round': request.round_number, 'revision': result.model_dump(mode='json')})
            return result

        try:
            returned = await asyncio.to_thread(revision_generator.generate, context, accept=accept, runtime=runtime)
            runtime.check_cancelled()
            if accepted is None or returned is not issued or returned != accepted:
                raise PlanningError('PLAN_SUBMISSION_MISSING', '修订未返回已接受结果')
            return accepted
        except Exception as exc:
            runtime.check_cancelled()
            raise DayRepairError(getattr(exc, 'code', 'REVISION_FAILED'), fatal=False) from None

    async def recalculate(request):
        runtime.check_cancelled()
        report = validate_daily_plan(request.planning_input, request.draft, request.facts,
            request.candidates, repair_states=request.repair_states)
        emit('repair.validation', {'day_id': request.day_id, 'round': request.round_number,
            'revision': request.draft.revision, 'report': report.model_dump(mode='json')})
        return DayRepairRecalculation(draft=request.draft, facts=request.facts, report=report)

    runtime.check_cancelled()
    try:
        result = await DayRepairService(collect=collect, revise=revise, recalculate=recalculate).run_async(
            snapshot.planning_input, draft, snapshot.facts, snapshot.candidates, snapshot.area_hints, snapshot.task_results, report)
    except asyncio.CancelledError:
        if getattr(runtime, 'cancel_event', None) is not None:
            runtime.cancel_event.set()
        raise
    runtime.check_cancelled()
    if any(state.stop_reason == 'technical_error' for state in result.repair_states):
        code = next((attempt.error_code for attempt in reversed(result.attempts)
                     if attempt.outcome == 'technical_error'), 'REPAIR_FAILED')
        return result.model_copy(update={'status': 'technical_error', 'stop_reason': 'technical_error', 'error_code': code})
    return result
