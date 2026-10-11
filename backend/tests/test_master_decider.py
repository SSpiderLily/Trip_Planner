"""F05a 主智能体单次派发决定的离线契约与安全回归。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from concurrent.futures import Future
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.agents.master_decider as master_decider_module
from app.agents.execution import PlanningError
from app.agents.master_decider import _DECISION_ADAPTER, MasterDecisionContext, MasterDecider
from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    DayConstraint,
    FactSource,
    FactStore,
    HardRequirement,
    MapCoordinate,
    PlaceFact,
    RouteFact,
    RouteOption,
    RouteSegment,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)
from app.models.planning_v4.input import PlanningInput
from app.models.planning_v4.tasks import Subtask, TaskResult, TaskScope
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository


TZ = "Asia/Shanghai"
DATES = tuple(date(2026, 10, 10) + timedelta(days=index) for index in range(3))
PERIODS = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
WINDOWS = tuple(
    {"period": period, "start_minute": start, "end_minute": end}
    for period, start, end in (
        ("breakfast", 8 * 60, 9 * 60),
        ("morning", 9 * 60, 12 * 60),
        ("lunch", 12 * 60, 14 * 60),
        ("afternoon", 14 * 60, 17 * 60),
        ("dinner", 17 * 60, 19 * 60),
        ("evening", 19 * 60, 22 * 60),
    )
)


def make_planning_input(*, lodging_query=None, arrival_place_query=None, departure_place_query=None,
                        named_restaurant=None, generic_must_include=None):
    remarks = "想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中；安排顺路午餐和晚餐。"
    hard_requirements = [HardRequirement(
        requirement_id="must-visit-bund",
        kind="must_visit",
        text="必须去外滩",
        source={"field": "remarks", "quote": "必须去外滩"},
    )]
    if named_restaurant:
        remarks += f"；必吃{named_restaurant}。"
        hard_requirements.append(HardRequirement(
            requirement_id="must-eat-restaurant",
            kind="must_include",
            text=f"必吃{named_restaurant}",
            source={"field": "remarks", "quote": f"必吃{named_restaurant}"},
        ))
    if generic_must_include:
        remarks += f"；{generic_must_include}。"
        hard_requirements.append(HardRequirement(
            requirement_id="generic-dining-requirement",
            kind="must_include",
            text=generic_must_include,
            source={"field": "remarks", "quote": generic_must_include},
        ))
    request = TripRequest(
        request_id="req-shanghai-3d",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone=TZ,
        preferences=("城市漫步", "美食"),
        transport_preferences=("transit", "walking"),
        lodging_query=lodging_query,
        arrival_place_query=arrival_place_query,
        departure_place_query=departure_place_query,
        remarks=remarks,
    )
    requirements = RequirementSummary(
        requirements_id="req-summary-shanghai",
        request_id=request.request_id,
        core_goals=("上海代表性景点", "城市漫步与美食"),
        hard_requirements=tuple(hard_requirements),
        preferences=("城市漫步", "美食"),
    )
    eligibility = (
        {"breakfast": "skipped", "morning": "skipped", "lunch": "required", "afternoon": "required", "dinner": "required", "evening": "required"},
        {period: "required" for period in PERIODS},
        {"breakfast": "required", "morning": "required", "lunch": "required", "afternoon": "required", "dinner": "conditional", "evening": "skipped"},
    )
    days = []
    for index, day_date in enumerate(DATES):
        day_id = f"day-{day_date.isoformat()}"
        slots = tuple(SlotConstraint(
            slot_id=f"slot-{day_date.isoformat()}-{period}",
            day_id=day_id,
            period=period,
            eligibility=eligibility[index][period],
            condition_reason="返程地点仍需核实" if eligibility[index][period] == "conditional" else None,
        ) for period in PERIODS)
        days.append(DayConstraint(day_id=day_id, day_date=day_date, slots=slots))
    rules = RuleSet(rule_version="shanghai-rules-v1", windows=WINDOWS)
    return PlanningInput(request=request, requirements=requirements, rules=rules, days=tuple(days))


def make_context(*, planning_input=None, facts=None, candidates=(), areas=(), tasks=(), results=(), **identity):
    planning_input = planning_input or make_planning_input()
    return MasterDecisionContext(
        planning_input=planning_input,
        fact_store=facts or FactStore(),
        candidates=tuple(candidates),
        area_hints=tuple(areas),
        existing_tasks=tuple(tasks),
        task_results=tuple(results),
        request_id=identity.get("request_id", planning_input.request.request_id),
        run_id=identity.get("run_id", "run-1"),
        context_revision=identity.get("context_revision", 4),
        base_revision=identity.get("base_revision"),
    )


def task_spec(local_ref, day_date, period, *, role="attractions", dependencies=(), requirements=(), target=2):
    return {
        "local_ref": local_ref,
        "role": role,
        "objective": "按用户要求核实代表性景点候选",
        "day_ids": [f"day-{day_date.isoformat()}"],
        "slot_ids": [f"slot-{day_date.isoformat()}-{period}"],
        "candidate_target": target,
        "requirement_ids": list(requirements),
        "dependencies": list(dependencies),
    }


def place_fact(*, fact_id="fact-bund", poi_id="poi-bund", name="外滩", lng=121.49, lat=31.24):
    source = FactSource(
        tool_call_id="call-bund",
        tool_name="maps_text_search",
        api_version="v3",
        queried_at=datetime(2026, 10, 10, 8, tzinfo=timezone.utc),
        effective_params={"city": "上海"},
    )
    fact = PlaceFact(
        fact_id=fact_id,
        poi_id=poi_id,
        name=name,
        city="上海市",
        location=MapCoordinate(lng=lng, lat=lat),
        cost={"raw": "约80元/人", "value": None},
        source_refs=(source.tool_call_id,),
        field_sources={"poi_id": source.tool_call_id, "name": source.tool_call_id, "location": source.tool_call_id},
    )
    return FactStore(sources=(source,), places=(fact,)), fact


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke_with_usage(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(kwargs)
        return response, SimpleNamespace(prompt_tokens=12, completion_tokens=8, total_tokens=20)


class MasterDeciderTests(unittest.TestCase):
    def make_decider(self, replies, timeout=2.0):
        llm = FakeLLM(replies)
        counter = iter(range(1, 1000))
        decider = MasterDecider(llm=llm, request_timeout_seconds=timeout,
                                id_factory=lambda: f"generated-{next(counter)}")
        return decider, llm

    def test_first_batch_can_delegate_independent_attractions_and_weather(self):
        payload = {
            "action": "delegate",
            "reason": "先核实景点候选并取得旅行日天气。",
            "tasks": [
                task_spec("arrival-afternoon", DATES[0], "afternoon"),
                task_spec("full-day-morning", DATES[1], "morning", requirements=("must-visit-bund",), target=1),
            ],
            "weather_dates": [item.isoformat() for item in DATES],
        }
        decider, llm = self.make_decider([json.dumps(payload, ensure_ascii=False)])
        result = decider.run(make_context())

        self.assertEqual(result.decision.action, "delegate")
        self.assertEqual(len(result.decision.tasks), 2)
        first, second = result.decision.tasks
        self.assertEqual(first.subtask.dependencies, ())
        self.assertEqual(second.subtask.requirement_ids, ("must-visit-bund",))
        self.assertEqual(second.subtask.constraints, ("必须去外滩",))
        self.assertEqual(result.decision.weather_dates, DATES)
        self.assertTrue(all(task.subtask.run_id == "run-1" for task in result.decision.tasks))
        self.assertTrue(all(task.subtask.context_revision == 4 for task in result.decision.tasks))
        self.assertIn(make_planning_input().request.remarks, llm.calls[0][0][1]["content"])
        self.assertEqual(llm.calls[0][1]["max_retries"], 0)
        self.assertLessEqual(llm.calls[0][1]["request_timeout"], 120)
        self.assertEqual(result.usage, {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20})

    def test_f03_equivalent_must_visit_task_uses_one_candidate_target(self):
        payload = {
            "action": "delegate",
            "reason": "核实必去外滩，并为下午找到景点候选。",
            "tasks": [task_spec(
                "bund-and-afternoon",
                DATES[0],
                "afternoon",
                requirements=("must-visit-bund",),
                target=1,
            )],
        }
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        result = decider.run(make_context())

        self.assertEqual(result.decision.tasks[0].subtask.candidate_target, 1)
        self.assertEqual(result.decision.tasks[0].subtask.requirement_ids, ("must-visit-bund",))

    def test_prompt_projects_only_role_eligible_slots_and_keeps_lodging_slotless(self):
        context = make_context()
        decider, _ = self.make_decider([])
        validated = decider._validate_context_data(context)
        payload = json.loads(decider._messages(context, validated)[1]["content"])
        scopes = payload["role_time_scopes"]
        all_day_ids = [day.day_id for day in context.planning_input.days]

        for scope in scopes.values():
            self.assertEqual(scope["allowed_day_ids"], all_day_ids)
        self.assertEqual(scopes["lodging"]["available_slots"], [])

        attraction_slots = scopes["attractions"]["available_slots"]
        self.assertTrue(attraction_slots)
        self.assertTrue(all(item["period"] in {"morning", "afternoon", "evening"}
                            for item in attraction_slots))
        self.assertNotIn("slot-2026-10-10-morning", {item["slot_id"] for item in attraction_slots})

        dining_slots = scopes["dining"]["available_slots"]
        self.assertTrue(dining_slots)
        self.assertTrue(all(item["period"] in {"breakfast", "lunch", "dinner"}
                            for item in dining_slots))
        self.assertNotIn("slot-2026-10-10-breakfast", {item["slot_id"] for item in dining_slots})
        self.assertIn(
            {"slot_id": "slot-2026-10-12-dinner", "day_id": "day-2026-10-12",
             "period": "dinner", "eligibility": "conditional"},
            dining_slots,
            "conditional 餐次可以使用，并应清楚投影其条件状态",
        )

        rules = payload["decision_rules"]
        self.assertEqual(rules["lodging"]["allowed_slots"], [])
        self.assertIn("必须为空", rules["lodging"]["slot_ids"])
        self.assertEqual(rules["lodging"]["candidate_target"], 1)
        self.assertIn("role_time_scopes.attractions.allowed_day_ids", rules["attractions"]["day_ids"])
        self.assertIn("role_time_scopes.dining.allowed_day_ids", rules["dining"]["day_ids"])
        self.assertIn("must_visit", rules["attractions"]["candidate_target"])
        self.assertIn("breakfast、lunch、dinner", rules["dining"]["allowed_slots"])
        system_prompt = decider._messages(context, validated)[0]["content"]
        self.assertIn("role_time_scopes", system_prompt)
        self.assertIn("conditional 时段可以使用", system_prompt)

    def test_role_slot_validator_rejects_wrong_role_skipped_and_mismatched_dates(self):
        invalid_scopes = (
            ("attractions", DATES[1], "lunch"),
            ("attractions", DATES[0], "morning"),
            ("dining", DATES[1], "morning"),
            ("dining", DATES[0], "breakfast"),
            ("lodging", DATES[0], "afternoon"),
            # slot ID 仍有效且角色时段正确，但 day_ids 指向另一天。
            ("attractions", DATES[0], "afternoon"),
        )
        for index, (role, slot_date, period) in enumerate(invalid_scopes):
            day_date = DATES[1] if index == len(invalid_scopes) - 1 else slot_date
            task = task_spec(
                f"invalid-slot-{index}", slot_date, period,
                role=role, target=1,
            )
            if day_date != slot_date:
                task["day_ids"] = [f"day-{day_date.isoformat()}"]
            decider, llm = self.make_decider([json.dumps({
                "action": "delegate", "reason": "slot资格测试", "tasks": [task],
            }, ensure_ascii=False)])

            with self.subTest(role=role, slot_date=slot_date, period=period, day_date=day_date):
                with self.assertRaises(PlanningError) as raised:
                    decider.run(make_context())
                self.assertEqual(raised.exception.code, "MASTER_TASK_SLOT_INVALID")
                self.assertEqual(len(llm.calls), 1)

    def test_second_batch_resolves_sourced_area_for_dining_and_lodging(self):
        planning_input = make_planning_input()
        facts, place = place_fact()
        previous_task = Subtask(
            task_id="task-attractions",
            run_id="run-1",
            context_revision=4,
            role="attractions",
            objective="景点检索",
            scope=TaskScope(city="上海", day_ids=(f"day-{DATES[1].isoformat()}",), slot_ids=(f"slot-{DATES[1].isoformat()}-morning",)),
            candidate_target=2,
        )
        previous_result = TaskResult(
            task_id=previous_task.task_id,
            run_id="run-1",
            context_revision=4,
            request_id=planning_input.request.request_id,
            status="partial",
            stop_reason="no_progress",
        )
        payload = {
            "action": "delegate",
            "reason": "已有真实位置依据，可以并行补齐餐饮和住宿候选。",
            "areas": [{
                "local_ref": "central-area",
                "label": "外滩附近",
                "anchor_poi_ids": [place.poi_id],
                "basis_fact_ids": [place.fact_id],
            }],
            "tasks": [
                {
                    "local_ref": "lunch-search",
                    "role": "dining",
                    "objective": "为午餐核实一家附近餐馆",
                    "day_ids": [f"day-{DATES[1].isoformat()}"],
                    "slot_ids": [f"slot-{DATES[1].isoformat()}-lunch"],
                    "area_refs": ["central-area"],
                    "known_fact_ids": [place.fact_id],
                    "candidate_target": 1,
                    "dependencies": [{"kind": "task", "reference": previous_task.task_id}],
                    "meal_areas": [{
                        "slot_id": f"slot-{DATES[1].isoformat()}-lunch",
                        "area_ref": "central-area",
                        "anchor_poi_id": place.poi_id,
                    }],
                },
                {
                    "local_ref": "lodging-search",
                    "role": "lodging",
                    "objective": "核实游览范围附近的住宿候选",
                    "day_ids": [day.day_id for day in planning_input.days],
                    "area_refs": ["central-area"],
                    "candidate_target": 1,
                },
            ],
        }
        decider, llm = self.make_decider([json.dumps(payload, ensure_ascii=False)])
        result = decider.run(make_context(
            planning_input=planning_input,
            facts=facts,
            tasks=(previous_task,),
            results=(previous_result,),
        ))

        self.assertEqual(result.decision.action, "delegate")
        hint = result.decision.areas[0]
        self.assertEqual(hint.basis_fact_ids, (place.fact_id,))
        self.assertEqual(hint.anchor_poi_ids, (place.poi_id,))
        self.assertEqual((hint.center.lng, hint.center.lat), (place.location.lng, place.location.lat))
        dining, lodging = result.decision.tasks
        self.assertEqual(dining.subtask.role, "dining")
        self.assertEqual(dining.meal_areas[0].area_hint_id, hint.area_hint_id)
        self.assertEqual(dining.meal_areas[0].anchor_poi_id, place.poi_id)
        self.assertTrue(any(dep.kind == "task" and dep.reference_id == previous_task.task_id
                            for dep in dining.subtask.dependencies),
                        "未完成的既有依赖应保留给 F05b 等待")
        self.assertEqual(lodging.subtask.scope.area_hint_ids, (hint.area_hint_id,))
        self.assertEqual(lodging.subtask.objective, "核实游览范围附近的住宿候选")
        prompt_facts = json.loads(llm.calls[0][0][1]["content"])["fact_store"]["places"]
        self.assertEqual(prompt_facts[0]["cost"]["raw"], place.cost.raw)
        self.assertTrue(any(dep.kind == "area_hint" and dep.reference_id == hint.area_hint_id
                            for dep in lodging.subtask.dependencies))

    def test_named_restaurant_is_bound_to_original_must_include_requirement(self):
        restaurant_name = "南翔馒头店"
        planning_input = make_planning_input(named_restaurant=restaurant_name)
        payload = {
            "action": "delegate",
            "reason": "核实用户明确指定的餐馆。",
            "tasks": [{
                "local_ref": "must-eat",
                "role": "dining",
                "objective": "核实必吃餐馆",
                "day_ids": [f"day-{DATES[1].isoformat()}"],
                "requirement_ids": ["must-eat-restaurant"],
                "candidate_target": 1,
                "must_eat_restaurants": [{
                    "requirement_id": "must-eat-restaurant",
                    "restaurant_name": restaurant_name,
                }],
            }],
        }
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        result = decider.run(make_context(planning_input=planning_input))

        resolved = result.decision.tasks[0]
        self.assertEqual(resolved.must_eat_restaurants[0].requirement_id, "must-eat-restaurant")
        self.assertEqual(resolved.must_eat_restaurants[0].restaurant_name, restaurant_name)
        self.assertIn("必吃南翔馒头店", resolved.subtask.constraints)

    def test_named_restaurant_must_be_in_original_requirement_quote(self):
        planning_input = make_planning_input(named_restaurant="南翔馒头店")
        payload = {
            "action": "delegate", "reason": "核实指定餐馆。", "tasks": [{
                "local_ref": "must-eat", "role": "dining", "objective": "核实指定餐馆",
                "day_ids": [f"day-{DATES[1].isoformat()}"],
                "requirement_ids": ["must-eat-restaurant"], "candidate_target": 1,
                "must_eat_restaurants": [{
                    "requirement_id": "must-eat-restaurant", "restaurant_name": "不存在的餐馆",
                }],
            }],
        }
        decider, llm = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(planning_input=planning_input))

        self.assertEqual(raised.exception.code, "MASTER_DINING_REQUIREMENT_INVALID")
        self.assertEqual(len(llm.calls), 1, "业务语义错误不应触发结构修复")

    def test_dining_requirement_id_requires_a_named_restaurant_binding(self):
        planning_input = make_planning_input(named_restaurant="南翔馒头店")
        payload = {
            "action": "delegate", "reason": "核实指定餐馆。", "tasks": [{
                "local_ref": "must-eat", "role": "dining", "objective": "核实指定餐馆",
                "day_ids": [f"day-{DATES[1].isoformat()}"],
                "requirement_ids": ["must-eat-restaurant"], "candidate_target": 1,
            }],
        }
        decider, llm = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(planning_input=planning_input))

        self.assertEqual(raised.exception.code, "MASTER_DINING_BINDING_INVALID")
        self.assertEqual(len(llm.calls), 1, "未绑定的命名餐馆是语义错误，不触发格式修复")

    def test_generic_dining_requirement_is_preserved_as_an_unverified_constraint(self):
        generic_requirement = "每天都要吃素"
        planning_input = make_planning_input(generic_must_include=generic_requirement)
        facts, place = place_fact()
        payload = {
            "action": "delegate",
            "reason": "按真实区域核实午餐候选。",
            "areas": [{
                "local_ref": "lunch-area", "label": "外滩附近",
                "anchor_poi_ids": [place.poi_id], "basis_fact_ids": [place.fact_id],
            }],
            "tasks": [{
                "local_ref": "lunch", "role": "dining", "objective": "核实适配用户要求的午餐候选",
                "day_ids": [f"day-{DATES[1].isoformat()}"],
                "slot_ids": [f"slot-{DATES[1].isoformat()}-lunch"],
                "area_refs": ["lunch-area"], "known_fact_ids": [place.fact_id],
                "candidate_target": 1,
                "meal_areas": [{
                    "slot_id": f"slot-{DATES[1].isoformat()}-lunch",
                    "area_ref": "lunch-area", "anchor_poi_id": place.poi_id,
                }],
            }],
        }
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        result = decider.run(make_context(planning_input=planning_input, facts=facts))

        resolved = result.decision.tasks[0]
        self.assertEqual(resolved.subtask.requirement_ids, ())
        self.assertEqual(resolved.must_eat_restaurants, ())
        self.assertIn(
            "饮食适配需有数据，未知不能承诺满足：" + generic_requirement,
            resolved.subtask.constraints,
        )

    def test_prompt_projects_actual_schema_and_valid_examples(self):
        context = make_context()
        decider, _ = self.make_decider([])
        validated = decider._validate_context_data(context)
        payload = json.loads(decider._messages(context, validated)[1]["content"])

        schema = payload["decision_json_schema"]
        self.assertEqual(schema, decider._decision_schema(context))
        self.assertIn("oneOf", schema)
        self.assertEqual(schema["discriminator"]["propertyName"], "action")
        self.assertEqual(
            schema["$defs"]["TaskSpec"]["properties"]["role"]["enum"],
            ["attractions"],
        )
        self.assertEqual(
            schema["$defs"]["TaskSpec"]["properties"]["candidate_target"]["type"],
            "integer",
        )
        self.assertNotIn("output_contract", payload)

        examples = payload["valid_output_examples"]
        self.assertEqual({item["action"] for item in examples}, {"delegate", "plan"})
        for example in examples:
            _DECISION_ADAPTER.validate_python(example)
            decider_for_example, _ = self.make_decider([json.dumps(example, ensure_ascii=False)])
            result = decider_for_example.run(context)
            self.assertEqual(result.decision.action, example["action"])
        delegate_example = next(item for item in examples if item["action"] == "delegate")
        self.assertEqual(delegate_example["tasks"][0]["candidate_target"], 1)
        self.assertEqual(delegate_example["tasks"][0]["requirement_ids"], ["must-visit-bund"])

    def test_user_lodging_query_has_no_area_scope_and_preserves_exact_text(self):
        query = "  上海外滩华尔道夫酒店  "
        planning_input = make_planning_input(lodging_query=query)
        payload = {
            "action": "delegate", "reason": "核实用户填写的住宿。", "tasks": [{
                "local_ref": "lodging", "role": "lodging", "objective": "模型提供的住宿查询目标",
                "day_ids": [day.day_id for day in planning_input.days], "candidate_target": 1,
                "area_refs": [],
            }],
        }
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        result = decider.run(make_context(planning_input=planning_input))

        task = result.decision.tasks[0].subtask
        self.assertEqual(task.scope.area_hint_ids, ())
        self.assertEqual(task.objective, "核实并使用用户填写的住处原文，不得替换为其他住宿。")
        self.assertIn("：" + query, task.constraints[-1])

    def test_user_lodging_query_rejects_recommendation_area(self):
        planning_input = make_planning_input(lodging_query="外滩华尔道夫")
        facts, place = place_fact()
        area = AreaHint(
            area_hint_id="existing-area", label="外滩周边", anchor_poi_ids=(place.poi_id,),
            center=place.location, basis_fact_ids=(place.fact_id,), basis="verified_place_facts",
        )
        payload = {
            "action": "delegate", "reason": "核实住宿。", "tasks": [{
                "local_ref": "lodging", "role": "lodging", "objective": "推荐附近住宿",
                "day_ids": [day.day_id for day in planning_input.days],
                "area_refs": [area.area_hint_id], "candidate_target": 1,
            }],
        }
        decider, llm = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(planning_input=planning_input, facts=facts, areas=(area,)))

        self.assertEqual(raised.exception.code, "MASTER_LODGING_SCOPE_INVALID")
        self.assertEqual(len(llm.calls), 1)

    def test_blank_lodging_query_is_treated_as_unset(self):
        planning_input = make_planning_input(lodging_query="  \t ")
        facts, place = place_fact()
        area = AreaHint(
            area_hint_id="existing-area", label="外滩周边", anchor_poi_ids=(place.poi_id,),
            center=place.location, basis_fact_ids=(place.fact_id,), basis="verified_place_facts",
        )
        payload = {
            "action": "delegate", "reason": "使用来源化区域推荐住宿。", "tasks": [{
                "local_ref": "lodging", "role": "lodging", "objective": "按区域推荐住宿",
                "day_ids": [day.day_id for day in planning_input.days],
                "area_refs": [area.area_hint_id], "candidate_target": 1,
            }],
        }
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        result = decider.run(make_context(planning_input=planning_input, facts=facts, areas=(area,)))

        task = result.decision.tasks[0].subtask
        self.assertEqual(task.objective, "按区域推荐住宿")
        self.assertEqual(task.scope.area_hint_ids, (area.area_hint_id,))
        self.assertFalse(any("用户指定住处原文" in item for item in task.constraints))

    def test_context_allows_same_poi_under_distinct_candidate_ids(self):
        facts, place = place_fact()
        candidates = (
            CandidateRef(candidate_id="candidate-day-one", category="sightseeing", poi_id=place.poi_id,
                         place_fact_id=place.fact_id, slot_ids=(f"slot-{DATES[0].isoformat()}-afternoon",)),
            CandidateRef(candidate_id="candidate-day-two", category="sightseeing", poi_id=place.poi_id,
                         place_fact_id=place.fact_id, slot_ids=(f"slot-{DATES[1].isoformat()}-morning",)),
        )
        decider, _ = self.make_decider([json.dumps({"action": "plan", "reason": "已有候选可以进入规划。"})])

        result = decider.run(make_context(facts=facts, candidates=candidates))

        self.assertEqual(result.decision.action, "plan")

    def test_conflicting_duplicate_candidate_id_is_rejected(self):
        facts, place = place_fact()
        first = CandidateRef(candidate_id="same-candidate", category="sightseeing", poi_id=place.poi_id,
                             place_fact_id=place.fact_id, recommendation_reason="first")
        conflicting = first.model_copy(update={"recommendation_reason": "different"})
        decider, llm = self.make_decider([])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(facts=facts, candidates=(first, conflicting)))

        self.assertEqual(raised.exception.code, "MASTER_CANDIDATE_INVALID")
        self.assertEqual(llm.calls, [])

    def test_prompt_sends_route_summary_without_geometry(self):
        facts, place = place_fact()
        source = facts.sources[0]
        route = RouteFact(
            fact_id="route-bund-to-museum", origin_poi_id=place.poi_id, destination_poi_id="poi-museum",
            origin=MapCoordinate(lng=121.49, lat=31.24), destination=MapCoordinate(lng=121.5, lat=31.25),
            mode="walking", status="available", source=source,
            options=(RouteOption(
                option_id="walk-option", duration_s=360, distance_m=450, geometry_status="complete",
                segments=(RouteSegment(sequence=0, mode="walking", duration_s=360, distance_m=450,
                                       polyline=(MapCoordinate(lng=121.491, lat=31.241),
                                                 MapCoordinate(lng=121.492, lat=31.242))),),
            ),),
        )
        route_facts = FactStore(sources=facts.sources, places=facts.places, routes=(route,))
        llm = FakeLLM([json.dumps({"action": "plan", "reason": "路线摘要已足够。"})])
        decider = MasterDecider(llm=llm, request_timeout_seconds=2)

        decider.run(make_context(facts=route_facts))

        prompt = llm.calls[0][0][1]["content"]
        prompt_data = json.loads(prompt)
        route_summary = prompt_data["fact_store"]["routes"][0]
        self.assertEqual(route_summary["options"][0]["duration_s"], 360)
        self.assertEqual(route_summary["origin_poi_id"], place.poi_id)
        self.assertNotIn("polyline", prompt)
        self.assertNotIn("121.491", prompt)

    def test_same_batch_dependency_is_resolved_to_backend_task_id(self):
        first = task_spec("first", DATES[0], "afternoon")
        second = task_spec(
            "second", DATES[1], "morning",
            dependencies=[{"kind": "task", "reference": "first"}],
        )
        decider, _ = self.make_decider([json.dumps({
            "action": "delegate", "reason": "按输入顺序执行", "tasks": [first, second],
        })])

        result = decider.run(make_context())
        first_id = result.decision.tasks[0].subtask.task_id
        second = result.decision.tasks[1].subtask
        self.assertIn(("task", first_id), {(item.kind, item.reference_id) for item in second.dependencies})
        self.assertNotEqual(first_id, second.task_id)

    def test_plan_is_only_a_transition_and_accepts_empty_or_failed_search_results(self):
        planning_input = make_planning_input()
        old_task = Subtask(
            task_id="task-empty",
            run_id="run-1",
            context_revision=4,
            role="attractions",
            objective="核实外滩",
            scope=TaskScope(city="上海", day_ids=(f"day-{DATES[0].isoformat()}",)),
            candidate_target=1,
        )
        failed = TaskResult(
            task_id=old_task.task_id,
            run_id="run-1",
            context_revision=4,
            request_id=planning_input.request.request_id,
            status="partial",
            stop_reason="no_progress",
        )
        decider, _ = self.make_decider([json.dumps({
            "action": "plan",
            "reason": "搜索已结束，进入独立规划阶段。",
            "missing_information": ["外滩周边餐馆尚未核实"],
        }, ensure_ascii=False)])
        result = decider.run(make_context(tasks=(old_task,), results=(failed,)))

        self.assertEqual(result.decision.action, "plan")
        self.assertEqual(result.decision.tasks, ())
        self.assertEqual(result.decision.areas, ())
        self.assertEqual(result.decision.missing_information, ("外滩周边餐馆尚未核实",))
        self.assertNotIn("days", result.decision.model_dump())
        self.assertEqual(result.decision.request_id, planning_input.request.request_id)

    def test_backend_identity_and_versions_cannot_be_overridden_by_model(self):
        decider, llm = self.make_decider([
            json.dumps({"action": "plan", "reason": "done", "request_id": "spoof"}),
            json.dumps({"action": "plan", "reason": "done", "missing_information": []}),
        ])
        result = decider.run(make_context(run_id="server-run", context_revision=7, base_revision=6))

        self.assertEqual(result.format_repairs, 1)
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(result.decision.request_id, "req-shanghai-3d")
        self.assertEqual(result.decision.run_id, "server-run")
        self.assertEqual(result.decision.context_revision, 7)
        self.assertEqual(result.decision.base_revision, 6)
        self.assertEqual(result.decision.rule_version, "shanghai-rules-v1")
        self.assertTrue(all(call[1]["max_retries"] == 0 for call in llm.calls))
        self.assertEqual(result.usage, {"input_tokens": 24, "output_tokens": 16, "total_tokens": 40})

    def test_same_batch_task_dependency_cycle_is_rejected_without_repair(self):
        one = task_spec("one", DATES[0], "afternoon", dependencies=[{"kind": "task", "reference": "two"}])
        two = task_spec("two", DATES[1], "morning", dependencies=[{"kind": "task", "reference": "one"}])
        decider, llm = self.make_decider([json.dumps({"action": "delegate", "reason": "cycle", "tasks": [one, two]})])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context())
        self.assertEqual(raised.exception.code, "MASTER_TASK_DEPENDENCY_CYCLE")
        self.assertEqual(len(llm.calls), 1)

    def test_unknown_task_or_fact_reference_is_rejected_as_semantic_error(self):
        task = task_spec("one", DATES[0], "afternoon", dependencies=[{"kind": "fact", "reference": "missing-fact"}])
        decider, llm = self.make_decider([json.dumps({"action": "delegate", "reason": "bad ref", "tasks": [task]})])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context())
        self.assertEqual(raised.exception.code, "MASTER_TASK_DEPENDENCY_UNKNOWN")
        self.assertEqual(len(llm.calls), 1, "业务语义错误不应消耗 JSON 修复额度")

    def test_dining_cannot_fabricate_area_anchor_or_coordinates(self):
        payload = {
            "action": "delegate",
            "reason": "bad anchor",
            "areas": [{"local_ref": "area", "label": "外滩周边", "anchor_poi_ids": ["invented-poi"], "basis_fact_ids": ["fact-bund"]}],
            "tasks": [{
                "local_ref": "meal",
                "role": "dining",
                "objective": "午餐搜索",
                "day_ids": [f"day-{DATES[1].isoformat()}"],
                "slot_ids": [f"slot-{DATES[1].isoformat()}-lunch"],
                "area_refs": ["area"],
                "candidate_target": 1,
                "meal_areas": [{
                    "slot_id": f"slot-{DATES[1].isoformat()}-lunch",
                    "area_ref": "area",
                    "anchor_poi_id": "invented-poi",
                }],
            }],
        }
        facts, _ = place_fact()
        decider, _ = self.make_decider([json.dumps(payload, ensure_ascii=False)])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(facts=facts))
        self.assertEqual(raised.exception.code, "MASTER_AREA_ANCHOR_INVALID")

    def test_endpoint_flags_require_original_user_locations(self):
        payload = {"action": "delegate", "reason": "verify endpoint", "verify_arrival_place": True}
        decider, _ = self.make_decider([json.dumps(payload)])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context())
        self.assertEqual(raised.exception.code, "MASTER_ENDPOINT_NOT_PROVIDED")

    def test_prompt_exposes_only_available_direct_queries(self):
        context = make_context()
        capabilities = MasterDecider._backend_query_capabilities(context)
        self.assertFalse(capabilities["verify_arrival_place"]["allowed"])
        self.assertFalse(capabilities["verify_departure_place"]["allowed"])
        self.assertEqual(capabilities["allowed_weather_dates"], [d.isoformat() for d in DATES])
        populated = context.model_copy(update={"planning_input": make_planning_input(
            arrival_place_query="   ", departure_place_query="上海虹桥站",
        )})
        capabilities = MasterDecider._backend_query_capabilities(populated)
        self.assertFalse(capabilities["verify_arrival_place"]["allowed"])
        self.assertTrue(capabilities["verify_departure_place"]["allowed"])
        self.assertEqual(capabilities["verify_departure_place"]["user_query"], "上海虹桥站")
        payload = {"action": "delegate", "reason": "核实端点", "verify_arrival_place": True}
        decider, _ = self.make_decider([json.dumps(payload)])
        with self.assertRaises(PlanningError) as raised:
            decider.run(populated)
        self.assertEqual(raised.exception.code, "MASTER_ENDPOINT_NOT_PROVIDED")

    def test_initial_decision_schema_excludes_roles_without_location_dependencies(self):
        context = make_context()
        decider = MasterDecider()
        payload = json.loads(decider._messages(context, decider._validate_context_data(context))[1]["content"])
        schema = payload["decision_json_schema"]
        self.assertEqual(schema["$defs"]["TaskSpec"]["properties"]["role"]["enum"], ["attractions"])
        self.assertEqual(schema["$defs"]["DelegatePayload"]["properties"]["verify_arrival_place"]["const"], False)
        self.assertEqual(schema["$defs"]["DelegatePayload"]["properties"]["verify_departure_place"]["const"], False)
        facts, _ = place_fact()
        populated = make_context(facts=facts)
        schema = json.loads(decider._messages(populated, decider._validate_context_data(populated))[1]["content"])["decision_json_schema"]
        self.assertEqual(schema["$defs"]["TaskSpec"]["properties"]["role"]["enum"], ["attractions", "dining", "lodging"])
        specified = context.model_copy(update={"planning_input": make_planning_input(lodging_query="上海某酒店")})
        schema = json.loads(decider._messages(specified, decider._validate_context_data(specified))[1]["content"])["decision_json_schema"]
        self.assertEqual(schema["$defs"]["TaskSpec"]["properties"]["role"]["enum"], ["attractions", "lodging"])

    def test_timeout_and_provider_errors_are_sanitized(self):
        decider, llm = self.make_decider([TimeoutError("raw timeout body")], timeout=0.1)
        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context())
        self.assertEqual(raised.exception.code, "MASTER_DECISION_TIMEOUT")
        self.assertNotIn("raw timeout body", str(raised.exception))
        self.assertLessEqual(llm.calls[0][1]["request_timeout"], 0.1)
        self.assertEqual(llm.calls[0][1]["max_retries"], 0)

        decider, _ = self.make_decider([RuntimeError("API_KEY=secret raw model body")])
        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context())
        self.assertEqual(raised.exception.code, "MASTER_LLM_FAILED")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("raw model body", str(raised.exception))

    def test_queued_model_call_is_cancelled_before_start_after_timeout(self):
        class QueuedExecutor:
            def __init__(self):
                self.submitted = None
                self.future = None

            def submit(self, fn, *args, **kwargs):
                self.submitted = (fn, args, kwargs)
                self.future = Future()
                return self.future

        queued = QueuedExecutor()
        decider, llm = self.make_decider([json.dumps({"action": "plan", "reason": "done"})], timeout=0.03)
        with patch.object(master_decider_module, "_MODEL_EXECUTOR", queued):
            with self.assertRaises(PlanningError) as raised:
                decider.run(make_context())

            self.assertEqual(raised.exception.code, "MASTER_DECISION_TIMEOUT")
            self.assertTrue(queued.future.cancelled())
            fn, args, kwargs = queued.submitted
            with self.assertRaises(TimeoutError):
                fn(*args, **kwargs)

        self.assertEqual(llm.calls, [], "排队超时后的工作线程不得启动模型调用")

    def test_observation_stores_only_safe_decision_summary_and_usage(self):
        raw_response = json.dumps({"action": "plan", "reason": "sensitive raw response marker", "missing_information": []})
        decider, _ = self.make_decider([raw_response])
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "master-observation.sqlite3")
            repository.initialize()
            repository.create("observation-task", {})
            repository.start("observation-task")
            with ObservationService(repository).task("observation-task"):
                result = decider.run(make_context())
            spans = repository.spans("observation-task")

        serialized = json.dumps(spans, ensure_ascii=False)
        self.assertNotIn("sensitive raw response marker", serialized)
        llm_span = next(item for item in spans if item["name"] == "master.decision.llm")
        self.assertEqual((llm_span["input_tokens"], llm_span["output_tokens"], llm_span["total_tokens"]), (12, 8, 20))
        self.assertEqual(result.decision.action, "plan")

    def test_provider_exception_is_redacted_inside_llm_observation_span(self):
        decider, _ = self.make_decider([RuntimeError("API_KEY=super-secret raw HTTP and model response")])
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "master-provider-error.sqlite3")
            repository.initialize()
            repository.create("observation-error-task", {})
            repository.start("observation-error-task")
            with ObservationService(repository).task("observation-error-task"):
                with self.assertRaises(PlanningError) as raised:
                    decider.run(make_context())
            spans = repository.spans("observation-error-task")
            llm_span_id = next(item["span_id"] for item in spans if item["name"] == "master.decision.llm")
            llm_span = repository.span("observation-error-task", llm_span_id)

        self.assertEqual(raised.exception.code, "MASTER_LLM_FAILED")
        serialized = json.dumps(spans, ensure_ascii=False)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("raw HTTP", serialized)
        self.assertEqual(llm_span["status"], "failed")
        self.assertEqual(llm_span["output_data"], {"status": "error"})

    def test_context_snapshot_versions_are_checked_before_model_call(self):
        stale_task = Subtask(
            task_id="old-task",
            run_id="run-other",
            context_revision=2,
            role="attractions",
            objective="stale",
            scope=TaskScope(city="上海", day_ids=(f"day-{DATES[0].isoformat()}",)),
            candidate_target=1,
        )
        decider, llm = self.make_decider([])

        with self.assertRaises(PlanningError) as raised:
            decider.run(make_context(tasks=(stale_task,)))
        self.assertEqual(raised.exception.code, "MASTER_TASK_STALE")
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main()
