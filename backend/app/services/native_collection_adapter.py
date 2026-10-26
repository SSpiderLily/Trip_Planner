"""P3阶段边界：原请求→有效摘要→输入→规范化资料；私有采集状态留在会话内。"""
from threading import Event, Lock
from app.models.planning_errors import PlanningError
from app.models.planning_evidence import WeatherEvidence, EndpointEvidence
from app.models.planning_execution import PlanningSnapshot, CollectionTaskNote
from app.models.planning_runtime import PlanningRunIdentity
from app.models.planning_v4 import PlanningInput, TripRequest
from app.planning.validation import validate_snapshot
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules


def _input(request, summary):
    from app.agents.native_requirement_interpreter import validate_native_requirement_summary
    validate_native_requirement_summary(request, summary)
    return build_planning_input(PlanningInputBuildRequest(request=request, requirements=summary,
        rules=default_planning_v4_rules()))


def _cancel(runtime, event):
    runtime.check_cancelled()
    if event is not None and event.is_set():
        raise PlanningError('CANCELLED', '采集已取消')


class NativeRequirementAdapter:
    def __init__(self, *, interpreter):
        self._interpreter = interpreter

    def run(self, request: TripRequest, *, runtime, cancel_event: Event | None = None) -> PlanningInput:
        request = TripRequest.model_validate(request.model_dump(mode='python'))
        _cancel(runtime, cancel_event)
        runtime.report('planning_input', message='整理旅行需求')
        interpreted = self._interpreter.run(request, cancel_event=cancel_event)
        _cancel(runtime, cancel_event)
        planning = _input(request, interpreted.summary)
        runtime.report('planning_input', 1, message='旅行需求已核对')
        _cancel(runtime, cancel_event)
        return planning


class NativeCollectionSession:
    """一个请求一个会话。缓存/补查状态不交给核心；初次采集与按日补查共用。"""
    def __init__(self, *, collector):
        self._collector = collector
        self._state = None
        self._snapshot = None
        self._binding = None
        self._lock = Lock()

    def collect(self, planning_input: PlanningInput, *, identity: PlanningRunIdentity,
                runtime, cancel_event: Event | None = None) -> PlanningSnapshot:
        # 不共享在途调用，避免同一请求同时执行两次采集。
        if not self._lock.acquire(blocking=False):
            raise PlanningError('COLLECTION_IN_PROGRESS', '采集会话正在执行')
        try:
            planning = PlanningInput.model_validate(planning_input.model_dump(mode='python'))
            identity = PlanningRunIdentity.model_validate(identity.model_dump(mode='python'))
            _cancel(runtime, cancel_event)
            if self._binding is not None and self._binding != (planning, identity):
                raise PlanningError('COLLECTION_SESSION_MISMATCH', '采集会话不能跨请求或运行复用')
            if _input(planning.request, planning.requirements) != planning:
                raise PlanningError('COLLECTION_INPUT_MISMATCH', '采集输入与需求转换不一致')
            self._binding = (planning.model_copy(deep=True), identity)
            if self._snapshot is not None:
                return self._snapshot.model_copy(deep=True)
            runtime.report('dispatch', message='采集地点与天气资料')
            try:
                state = self._collector.run(planning.request, requirements=planning.requirements,
                    run_id=identity.task_id, cancel_event=cancel_event)
            except Exception as exc:
                _cancel(runtime, cancel_event)
                code = getattr(exc, 'code', None)
                raise PlanningError(code if isinstance(code, str) else 'COLLECTION_FAILED', '资料采集失败') from None
            _cancel(runtime, cancel_event)
            if state.run_id != identity.task_id or state.planning_input != planning:
                raise PlanningError('COLLECTION_INPUT_MISMATCH', '采集身份或输入不一致')
            if state.weather is None or state.endpoints is None:
                raise PlanningError('COLLECTION_EVIDENCE_MISSING', '采集缺少天气或到离地点验证资料')
            weather = WeatherEvidence.model_validate({name: getattr(state.weather, name)
                for name in ('run_id', 'planning_input', 'executions', 'fact_store')})
            endpoints = EndpointEvidence.model_validate({name: getattr(state.endpoints, name)
                for name in ('run_id', 'planning_input', 'executions', 'arrival', 'departure', 'fact_store')})
            value = PlanningSnapshot(identity=identity, planning_input=planning, facts=state.fact_store,
                candidates=state.candidates, area_hints=state.area_hints,
                task_results=tuple(child.task_result for child in state.children),
                task_notes=tuple(CollectionTaskNote(task_id=child.task_result.task_id, unmet_needs=child.unmet_needs)
                    for child in state.children), weather_evidence=weather, endpoint_evidence=endpoints)
            validate_snapshot(value)
            runtime.report('dispatch', 1, message='采集资料已核对')
            _cancel(runtime, cancel_event)
            # 仅成功并且未取消的调用发布状态；失败不能污染下一次执行。
            self._state, self._snapshot = state, value.model_copy(deep=True)
            return value
        finally:
            self._lock.release()

    @property
    def task_notes(self):
        return self._snapshot.task_notes if self._snapshot is not None else ()

    def supplement(self, request, *, runtime, cancel_event=None):
        """补查状态只在会话内推进；身份、已有事实和取消全部核对后发布。"""
        from dataclasses import replace
        from app.services.day_repair_service import (
            DayRepairCollectionResult, DayRepairInputError, _collection_scope, _merge_facts, _merge_by_id,
        )
        from app.planning.rules import DailyPlanRules
        if not self._lock.acquire(blocking=False):
            raise PlanningError('COLLECTION_IN_PROGRESS', '采集会话正在执行')
        try:
            _cancel(runtime, cancel_event)
            if self._snapshot is None or self._state is None:
                raise PlanningError('COLLECTION_SESSION_MISMATCH', '补查需要初次采集会话')
            planning, identity = self._binding
            if (request.planning_input != planning or request.draft.plan_id != identity.plan_id
                    or request.draft.source_run_id != identity.task_id
                    or request.draft.request_id != planning.request.request_id
                    or request.day not in request.draft.days):
                raise PlanningError('COLLECTION_SESSION_MISMATCH', '补查输入不属于本次运行')
            constraint = next(d for d in planning.days if d.day_id == request.day.day_id)
            _, expected = _collection_scope(planning, constraint, request.day, request.issues,
                                            request.fact_store, request.candidates)
            if not expected or request.targets != expected:
                raise PlanningError('REPAIR_TASK_OUTSIDE_SCOPE', '补查范围必须对应目标日真实缺项')
            # 既有事实不可重写，所有任务必须保持引用闭合。
            try:
                if (_merge_facts(self._snapshot.facts, request.fact_store) != request.fact_store
                        or _merge_by_id(self._snapshot.candidates, request.candidates, 'candidate_id', 'candidate') != request.candidates):
                    raise DayRepairInputError('stale collection snapshot')
                DailyPlanRules._validate_input_references(planning, request.candidates, request.fact_store,
                    request.area_hints, request.task_results, identity.task_id)
            except (DayRepairInputError, ValueError) as exc:
                raise PlanningError('DAY_REPAIR_FACT_CONFLICT', '补查输入事实冲突') from None
            seed = replace(self._state, fact_store=request.fact_store.model_copy(deep=True),
                candidates=request.candidates, area_hints=request.area_hints)
            previous_count = len(seed.children)
            try:
                state = self._collector.run(planning.request, initial_state=seed, repair_request=request,
                    requirements=planning.requirements, run_id=identity.task_id, cancel_event=cancel_event)
            except Exception as exc:
                _cancel(runtime, cancel_event)
                raise PlanningError(getattr(exc, 'code', 'REPAIR_COLLECTION_FAILED'), '补查失败') from None
            _cancel(runtime, cancel_event)
            if state.run_id != identity.task_id or state.planning_input != planning:
                raise PlanningError('COLLECTION_IDENTITY_MISMATCH', '补查身份或需求不一致')
            value = self._snapshot.model_copy(update={
                'facts': state.fact_store, 'candidates': state.candidates, 'area_hints': state.area_hints,
                'task_results': tuple(c.task_result for c in state.children),
                'task_notes': tuple(CollectionTaskNote(task_id=c.task_result.task_id, unmet_needs=c.unmet_needs)
                                    for c in state.children)})
            value = PlanningSnapshot.model_validate(value.model_dump(mode='python'))
            validate_snapshot(value)
            try:
                _merge_facts(request.fact_store, value.facts)
                _merge_by_id(request.candidates, value.candidates, 'candidate_id', 'candidate')
                _merge_by_id(request.area_hints, value.area_hints, 'area_hint_id', 'area')
            except DayRepairInputError:
                raise PlanningError('DAY_REPAIR_FACT_CONFLICT', '补查覆盖已有事实或候选') from None
            # 只校验本轮新增候选；既有候选的跨日复用规则不变。
            existing_ids = {candidate.candidate_id for candidate in request.candidates}
            allowed_by_category = {
                category: {slot_id for target in request.targets if target.role == role
                           for slot_id in target.slot_ids}
                for category, role in (('meal', 'dining'), ('sightseeing', 'attractions'))
            }
            for candidate in value.candidates:
                if candidate.candidate_id in existing_ids:
                    continue
                allowed = allowed_by_category.get(candidate.category, set())
                # 原生景点是未分配时段的地点池；不能据空标签认定越界。
                if candidate.category == 'sightseeing' and allowed and not candidate.slot_ids:
                    continue
                if not candidate.slot_ids or not set(candidate.slot_ids).issubset(allowed):
                    raise PlanningError('REPAIR_TASK_OUTSIDE_SCOPE', '新增候选超出本次补查范围')
            _, remaining = _collection_scope(planning, constraint, request.day, request.issues, value.facts, value.candidates)
            children = state.children[previous_count:]
            failed = any(c.task_result.stop_reason == 'model_error' for c in children)
            map_failed = bool(remaining) and any(_map_failed(c) for c in children)
            no_dispatch = not children and bool(remaining)
            outcome = 'technical_error' if failed or map_failed or no_dispatch else 'no_progress' if remaining else 'target_met'
            _cancel(runtime, cancel_event)
            self._state, self._snapshot = state, value.model_copy(deep=True)
            return DayRepairCollectionResult(stop_reason=outcome, fact_store=value.facts,
                candidates=value.candidates, area_hints=value.area_hints, task_results=value.task_results,
                error_code='MODEL_CALL_FAILED' if failed else 'REPAIR_MAP_FAILED' if map_failed
                           else 'REPAIR_DID_NOT_DELEGATE' if no_dispatch else None)
        finally:
            self._lock.release()


def _map_failed(child):
    import json
    if child.task_result.status == 'complete' or child.protocol is None:
        return False
    for message in child.protocol.messages:
        if message.get('role') == 'tool':
            value = json.loads(message['content'])
            if value.get('data', {}).get('result_status') == 'error':
                return True
    return False
