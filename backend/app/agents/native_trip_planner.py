"""正式编排：需求→公开采集快照→独立初版→按日修订→公开结果。"""
from __future__ import annotations

from app.services.native_observation import native_event_emitter

import asyncio
from threading import Event
from app.agents.native_multiagent_collection import NativeMultiagentCollection
from app.agents.native_requirement_interpreter import NativeRequirementInterpreter
from app.models.planning_v4 import TripRequest
from app.models.planning_runtime import ProgressCallback
from app.services.llm_service import get_agent_llm
from app.services.native_tool_loop import NativeToolLoopError
from app.services.native_planning_runtime import NativePlanningRuntime
from app.agents.native_daily_plan_agent import SubmitDailyPlanArguments
from app.agents.native_day_revision_agent import SubmitDayRevisionArguments


class NativeTripPlanner:
    def __init__(self, *, master_llm=None, collector=None, attraction_llm=None, dining_llm=None,
                 lodging_llm=None, adapter=None, tool_executor=None, emit=None,
                 max_plan_model_calls=5, max_plan_tool_calls=4, requirement_interpreter=None, weather_service=None,
                 endpoint_service=None):
        self.master_llm = master_llm
        self.emit = native_event_emitter(emit)
        self.requirement_interpreter = requirement_interpreter or NativeRequirementInterpreter(llm=master_llm, emit=self.emit)
        self.collector = collector or NativeMultiagentCollection(master_llm=master_llm,
            attraction_llm=attraction_llm, dining_llm=dining_llm, lodging_llm=lodging_llm,
            adapter=adapter, tool_executor=tool_executor, emit=self.emit, weather_service=weather_service,
            endpoint_service=endpoint_service)
        self.max_plan_model_calls, self.max_plan_tool_calls = max_plan_model_calls, max_plan_tool_calls
        # 保留旧调用方的纯规则兼容入口。
        from app.planning.rules import DailyPlanRules
        self.contract = DailyPlanRules()

    def run(self, request: TripRequest, *, task_id: str | None = None, plan_id: str | None = None,
            progress: ProgressCallback | None = None, cancel_event: Event | None = None):
        return asyncio.run(self.run_async(request, task_id=task_id, plan_id=plan_id,
                                         progress=progress, cancel_event=cancel_event))

    async def run_async(self, request: TripRequest, *, task_id: str | None = None, plan_id: str | None = None,
                        progress: ProgressCallback | None = None, cancel_event: Event | None = None):
        runtime = NativePlanningRuntime(task_id=task_id, plan_id=plan_id, progress=progress,
                                        cancel_event=cancel_event)
        try:
            return await self._run(request, runtime)
        except Exception as exc:
            runtime.cancel_event.set()
            from app.models.planning_errors import PlanningError
            if isinstance(exc, PlanningError):
                code = "COLLECTION_IDENTITY_MISMATCH" if exc.code == "COLLECTION_INPUT_MISMATCH" else exc.code
                raise NativeToolLoopError(code) from None
            raise
        except BaseException:
            runtime.cancel_event.set()
            raise

    async def _run(self, request, runtime):
        from uuid import uuid4
        from app.models.planning_runtime import PlanningRunIdentity
        from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
        from app.agents.native_day_revision_agent import NativeDayRevisionAgent
        from app.planning.core import generate_draft
        from app.services.native_collection_adapter import NativeCollectionSession
        from app.services.planning_runtime_adapter import NativePlanningRuntimeAdapter
        from app.services.planning_repair_service import repair_draft
        if not isinstance(request, TripRequest):
            raise NativeToolLoopError('TRIP_REQUEST_INVALID')
        runtime.check_cancelled()
        from app.services.native_collection_adapter import NativeRequirementAdapter
        # 一次任务捕获一套装配，只通过公开阶段端口传递值。
        identity = runtime.identity or PlanningRunIdentity(task_id='run_'+uuid4().hex, plan_id='plan_'+uuid4().hex)
        bridge = NativePlanningRuntimeAdapter(runtime)
        requirements = NativeRequirementAdapter(interpreter=self.requirement_interpreter)
        planning = await runtime.blocking(requirements.run, request, runtime=bridge, cancel_event=runtime.cancel_event)
        session = NativeCollectionSession(collector=self.collector)
        snapshot = await runtime.blocking(session.collect, planning, identity=identity,
                                          runtime=bridge, cancel_event=runtime.cancel_event)
        master = self.master_llm or get_agent_llm('master')
        generator = NativeDailyPlanAgent(master, max_model_calls=self.max_plan_model_calls,
                                        max_tool_calls=self.max_plan_tool_calls, emit=self.emit)
        try:
            initial = await runtime.blocking(generate_draft, snapshot, generator=generator, runtime=bridge)
        except NativeToolLoopError as exc:
            if exc.code == 'PLAN_SUBMISSION_MISSING':
                raise NativeToolLoopError('MASTER_DID_NOT_SUBMIT_PLAN') from None
            raise
        self.emit('planning.initial', {'draft': initial.draft.model_dump(mode='json'),
                                      'report': initial.report.model_dump(mode='json')})
        runtime.check_cancelled()
        await runtime.report('repair', message='检查必需地点缺项并按日修订')
        # 修订会话在工作线程中运行；进度沿用原异步运行环境回传。
        def repair():
            return asyncio.run(repair_draft(snapshot, initial.draft, initial.report,
                revision_generator=NativeDayRevisionAgent(master, max_model_calls=self.max_plan_model_calls,
                    max_tool_calls=self.max_plan_tool_calls, emit=self.emit), collection=session, runtime=bridge, emit=self.emit))
        repaired = await runtime.blocking(repair)
        await runtime.report('repair', 1, message='地点规划阶段已结束')
        from app.models.planning_outcome import PlanningOutcome
        from app.models.planning_execution import PlanningSnapshot
        from app.services.planning_diagnostics import safe_error_code
        final_snapshot = PlanningSnapshot.model_validate({**snapshot.model_dump(mode='python'),
            'facts': repaired.facts, 'candidates': repaired.candidates, 'area_hints': repaired.area_hints,
            'task_results': repaired.task_results, 'task_notes': session.task_notes})
        result = PlanningOutcome(status=repaired.status,
            error_code=safe_error_code(repaired.error_code) if repaired.error_code else None,
            initial_result=initial, snapshot=final_snapshot, repair=repaired)
        self.emit('planning.result', {'status': repaired.status, 'draft': repaired.draft.model_dump(mode='json'),
            'report': repaired.report.model_dump(mode='json'),
            'repair_states': [item.model_dump(mode='json') for item in repaired.repair_states],
            'attempts': [item.model_dump(mode='json') for item in repaired.attempts]})
        return result
