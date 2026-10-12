"""同一主智能体：原生采集→每日提案→地点校验→按日补查/修订。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import json
from threading import Event

from pydantic import Field

from app.agents.daily_plan_activity_repair import DailyPlanModelOutput
from app.agents.daily_plan_generator import DailyPlanGenerator, _InvalidReferences
from app.agents.execution import PlanningError
from app.agents.native_multiagent_collection import NativeMultiagentCollection, NativeMultiagentCollectionResult
from app.models.planning_v4 import PlanDraft, PlanningInput, ProposalDay, DayRevision, TripRequest
from app.services.day_repair_service import (
    DayRepairService, DayRepairResult, DayRepairError, DayRepairCollectionResult,
    DayRepairRecalculation, apply_day_revision, _collection_scope,
)
from app.services.llm_service import get_agent_llm
from app.services.native_tool_loop import NativeTool, NativeToolArguments, NativeToolLoop, NativeToolLoopError
from app.services.planning_v4_validation import validate_daily_plan, is_required_place_gap
from app.services.planning_v4_validator import ContractValidationError


class SubmitDailyPlanArguments(NativeToolArguments):
    days: list[ProposalDay] = Field(min_length=1)
    missing_notes: list[str] = Field(default_factory=list, max_length=40)


class SubmitDayRevisionArguments(NativeToolArguments):
    replacement_day: ProposalDay


@dataclass(frozen=True)
class NativeTripPlanResult:
    planning_input: PlanningInput
    initial_draft: PlanDraft
    repair: DayRepairResult
    collection: NativeMultiagentCollectionResult = field(repr=False)
    master_history: tuple[dict, ...] = field(repr=False)

    @property
    def draft(self):
        return self.repair.draft

    @property
    def fact_store(self):
        return self.repair.facts

    @property
    def report(self):
        return self.repair.report


def _reference_codes(exc):
    if isinstance(exc, ContractValidationError):
        return sorted({issue.code for issue in exc.issues})
    if isinstance(exc, _InvalidReferences):
        return list(exc.codes)
    if isinstance(exc, PlanningError):
        return [exc.code]
    return ["PLAN_SUBMISSION_INVALID"]


def _map_failed(child):
    if child.task_result.status == "complete" or child.protocol is None:
        return False
    for message in child.protocol.messages:
        if message.get("role") != "tool":
            continue
        value = json.loads(message["content"])
        if value.get("data", {}).get("result_status") == "error":
            return True
    return False


class NativeTripPlanner:
    def __init__(self, *, master_llm=None, collector=None, attraction_llm=None, dining_llm=None,
                 lodging_llm=None, adapter=None, tool_executor=None, emit=None,
                 max_plan_model_calls=5, max_plan_tool_calls=4):
        self.master_llm = master_llm
        self.emit = emit or (lambda event, data: None)
        self.collector = collector or NativeMultiagentCollection(master_llm=master_llm,
            attraction_llm=attraction_llm, dining_llm=dining_llm, lodging_llm=lodging_llm,
            adapter=adapter, tool_executor=tool_executor, emit=self.emit)
        self.max_plan_model_calls, self.max_plan_tool_calls = max_plan_model_calls, max_plan_tool_calls
        # 只使用旧生成器的纯契约/事实绑定能力；不调用它的模型决策或旧派发器。
        self.contract = DailyPlanGenerator()

    def run(self, request: TripRequest, *, cancel_event: Event | None = None):
        return asyncio.run(self.run_async(request, cancel_event=cancel_event))

    async def run_async(self, request: TripRequest, *, cancel_event: Event | None = None):
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise NativeToolLoopError("CANCELLED")
        cancelled()
        state = await asyncio.to_thread(self.collector.run, request, cancel_event=cancel_event)
        if state.planning_input is None or state.protocol is None:
            raise NativeToolLoopError("COLLECTION_STATE_INCOMPLETE")
        planning = state.planning_input
        history = list(state.protocol.messages)
        master = self.master_llm or get_agent_llm("master")
        results = tuple(child.task_result for child in state.children)
        self.contract._validate_input_references(planning, state.candidates, state.fact_store,
                                                 state.area_hints, results, state.run_id)
        accepted = None

        def submit_plan(args):
            nonlocal accepted
            cancelled()
            if accepted is not None:
                return {"status": "already_submitted"}
            try:
                output = DailyPlanModelOutput.model_validate(args.model_dump(mode="python"))
                proposal = self.contract._validate_proposal_output(output, planning,
                    state.candidates, state.fact_store, state.area_hints)
                draft = self.contract._build_draft(proposal, planning, state.candidates,
                    state.fact_store, state.area_hints, state.run_id)
                report = validate_daily_plan(planning, draft, state.fact_store, state.candidates)
            except (ContractValidationError, PlanningError, _InvalidReferences, ValueError) as exc:
                codes = _reference_codes(exc)
                self.emit("planning.submission_rejected", {"codes": codes})
                return {"status": "rejected", "code": "PLAN_REFERENCE_INVALID", "codes": codes}
            cancelled()
            accepted = draft, report
            self.emit("planning.initial", {"draft": draft.model_dump(mode="json"),
                                           "report": report.model_dump(mode="json")})
            return {"status": "accepted", "revision": draft.revision,
                    "required_place_gaps": [issue.model_dump(mode="json") for issue in report.issues
                        if is_required_place_gap(planning, issue)],
                    "instruction": "初版已接收；后端下一阶段会按日修订缺项。不要重提或编造已修复结论。"}

        messages = self.contract._messages(planning, state.candidates, state.fact_store, state.area_hints, results)
        messages[0]["content"] = ("采集阶段结束，当前切换为每日规划；此前仅采集限制结束。\n" +
            messages[0]["content"] + "\n本阶段覆盖普通文本JSON说明：必须调用submit_daily_plan提交days/missing_notes；普通文本只做简短确认。校验拒绝后按codes修正，只选择已核实候选。不得改住宿或天气事实，不输出精确活动时间、费用或交通。\n餐次按用途基本判断，早餐优先店名或类别表明早餐、早点、面点的候选，不把普通正餐餐厅默认当早餐店。相近景区/建筑群及其子地标可能覆盖同一体验，不能仅凭不同POI ID声称避免跨天重复。未查询路线时只能说明锚点周边范围，不承诺步行可达。")
        self.emit("planning.phase", {"phase": "initial", "candidate_count": len(state.candidates)})
        protocol = await asyncio.to_thread(NativeToolLoop(master, [NativeTool("submit_daily_plan",
            "提交每日安排days和missing_notes；活动只引用candidate_id，日期/时段照输入，不提供地点事实、价格、时间或版本。",
            SubmitDailyPlanArguments, submit_plan)], max_model_calls=self.max_plan_model_calls,
            max_tool_calls=self.max_plan_tool_calls).run, [*history, *messages], cancel_event=cancel_event)
        history = list(protocol.messages)
        cancelled()
        if accepted is None:
            raise NativeToolLoopError("MASTER_DID_NOT_SUBMIT_PLAN")
        initial_draft, report = accepted

        async def collect(request_data):
            nonlocal state, history
            cancelled()
            self.emit("repair.collect", {"day_id": request_data.day.day_id, "round": request_data.round_number,
                "targets": [target.model_dump(mode="json") for target in request_data.targets]})
            seed = replace(state, planning_input=planning, fact_store=request_data.fact_store,
                candidates=request_data.candidates, area_hints=request_data.area_hints)
            previous_count = len(seed.children)
            try:
                collected = await asyncio.to_thread(self.collector.run, request, initial_state=seed,
                    repair_request=request_data, messages=history, cancel_event=cancel_event)
            except NativeToolLoopError as exc:
                raise DayRepairError(exc.code, fatal=exc.code == "CANCELLED") from None
            cancelled()
            if collected.planning_input.requirements != planning.requirements:
                raise DayRepairError("REPAIR_REQUIREMENTS_CHANGED", fatal=True)
            try:
                self.contract._validate_input_references(planning, collected.candidates, collected.fact_store,
                    collected.area_hints, tuple(child.task_result for child in collected.children), collected.run_id)
            except PlanningError as exc:
                raise DayRepairError(exc.code, fatal=True) from None
            state = collected
            history = list(collected.protocol.messages)
            _, remaining = _collection_scope(planning,
                next(day for day in planning.days if day.day_id == request_data.day.day_id),
                request_data.day, request_data.issues, collected.fact_store, collected.candidates)
            failed = any(child.task_result.stop_reason == "model_error"
                         for child in collected.children[previous_count:])
            map_failed = bool(remaining) and any(_map_failed(child) for child in collected.children[previous_count:])
            no_dispatch = len(collected.children) == previous_count and bool(remaining)
            outcome = "technical_error" if failed or map_failed or no_dispatch else "no_progress" if remaining else "target_met"
            self.emit("repair.collected", {"day_id": request_data.day.day_id, "stop_reason": outcome,
                                          "remaining_targets": len(remaining)})
            return DayRepairCollectionResult(stop_reason=outcome, fact_store=collected.fact_store,
                candidates=collected.candidates, area_hints=collected.area_hints,
                task_results=tuple(child.task_result for child in collected.children),
                error_code="MODEL_CALL_FAILED" if failed else "REPAIR_MAP_FAILED" if map_failed
                           else "REPAIR_DID_NOT_DELEGATE" if no_dispatch else None)

        async def revise(request_data):
            nonlocal history
            cancelled()
            proposed = None
            def submit_revision(args):
                nonlocal proposed
                cancelled()
                if proposed is not None:
                    return {"status": "already_submitted"}
                try:
                    revision = DayRevision(day_id=request_data.day.day_id,
                        base_revision=request_data.draft.revision, revision=request_data.draft.revision + 1,
                        based_on_report_revision=request_data.report.based_on_revision,
                        replacement_day=args.replacement_day, preserve_activity_ids=request_data.preserve_activity_ids,
                        source_issue_ids=tuple(issue.issue_id for issue in request_data.issues))
                    self.contract._validate_revision_candidates(revision, planning, request_data.candidates,
                        request_data.fact_store, request_data.area_hints,
                        request_data.preserve_activity_ids, request_data.modifiable_activity_ids)
                    apply_day_revision(revision, request_data.draft, request_data.report, planning,
                        request_data.candidates, request_data.fact_store,
                        expected_day_id=request_data.day.day_id,
                        expected_source_issue_ids=revision.source_issue_ids,
                        preserve_activity_ids=request_data.preserve_activity_ids,
                        allowed_change_activity_ids=request_data.modifiable_activity_ids,
                        removable_activity_ids=request_data.removable_activity_ids)
                except (ContractValidationError, PlanningError, _InvalidReferences, ValueError) as exc:
                    codes = _reference_codes(exc)
                    self.emit("repair.submission_rejected", {"day_id": request_data.day.day_id, "codes": codes})
                    return {"status": "rejected", "code": "REVISION_REFERENCE_INVALID", "codes": codes}
                cancelled()
                proposed = revision
                self.emit("repair.revision", {"round": request_data.round_number, "revision": revision.model_dump(mode="json")})
                return {"status": "accepted", "day_id": revision.day_id, "revision": revision.revision}

            constraint = next(day for day in planning.days if day.day_id == request_data.day.day_id)
            messages = self.contract._revision_messages(planning, request_data.draft, request_data.report,
                request_data.day, constraint, request_data.issues, request_data.assignments,
                request_data.candidates, request_data.fact_store, request_data.area_hints, request_data.task_results,
                request_data.preserve_activity_ids, request_data.modifiable_activity_ids, request_data.removable_activity_ids,
                request_data.round_id, request_data.round_number)
            messages[0]["content"] += "\n当前阶段切换为目标日修订，覆盖此前初版提交/普通文本JSON规则。必须调用submit_day_revision提交replacement_day，校验拒绝后纠正；通过后普通文本仅确认。"
            self.emit("planning.phase", {"phase": "revision", "day_id": request_data.day.day_id,
                                        "round": request_data.round_number})
            try:
                protocol = await asyncio.to_thread(NativeToolLoop(master, [NativeTool("submit_day_revision",
                    "只提交目标日期replacement_day，保留既有活动ID与相对顺序；运行版本和问题ID由后端绑定。",
                    SubmitDayRevisionArguments, submit_revision)], max_model_calls=self.max_plan_model_calls,
                    max_tool_calls=self.max_plan_tool_calls).run, [*history, *messages], cancel_event=cancel_event)
            except NativeToolLoopError as exc:
                raise DayRepairError(exc.code, fatal=exc.code == "CANCELLED") from None
            history = list(protocol.messages)
            cancelled()
            if proposed is None:
                raise DayRepairError("MASTER_DID_NOT_SUBMIT_REVISION")
            return proposed

        async def recalculate(request_data):
            cancelled()
            report = validate_daily_plan(planning, request_data.draft, request_data.facts,
                request_data.candidates, repair_states=request_data.repair_states)
            self.emit("repair.validation", {"day_id": request_data.day_id, "round": request_data.round_number,
                "revision": request_data.draft.revision, "report": report.model_dump(mode="json")})
            return DayRepairRecalculation(draft=request_data.draft, facts=request_data.facts, report=report)

        repaired = await DayRepairService(collect=collect, revise=revise, recalculate=recalculate).run_async(
            planning, initial_draft, state.fact_store, state.candidates, state.area_hints, results, report)
        cancelled()
        result = NativeTripPlanResult(planning, initial_draft, repaired, state, tuple(history))
        self.emit("planning.result", {"status": repaired.status, "draft": repaired.draft.model_dump(mode="json"),
            "report": repaired.report.model_dump(mode="json"),
            "repair_states": [item.model_dump(mode="json") for item in repaired.repair_states],
            "attempts": [item.model_dump(mode="json") for item in repaired.attempts]})
        return result
