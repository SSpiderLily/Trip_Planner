"""F06 每日安排生成：只用合成事实和替身模型进行离线测试。"""
from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from app.agents.daily_plan_generator import DailyPlanGenerator, DailyPlanModelOutput
from app.agents.daily_plan_activity_repair import parse_patch_response
from app.agents.execution import PlanningError
from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    FactStore,
    FactSource,
    HardRequirement,
    MapCoordinate,
    PlaceFact,
    PlanProposal,
    PlanningInput,
    ProposalActivity,
    ProposalDay,
    RequirementSummary,
    RuleSet,
    TripRequest,
    TaskResult,
    WeatherFact,
    WeatherForecast,
)
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.models.planning_v4.input import RequirementSource
from app.services.llm_service import LLMCompletion
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from app.services.planning_v4_validation import validate_daily_plan


TZ = "Asia/Shanghai"
RUN_ID = "run-f06-synthetic"
WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke_with_usage(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}


class MetadataFakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke_with_metadata(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response, finish_reason = self.responses.pop(0)
        return LLMCompletion(
            text=response,
            usage={"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
            finish_reason=finish_reason,
        )


def make_input(*, request_id="request-f06", lodging_query=None):
    request = TripRequest(
        request_id=request_id,
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone=TZ,
        preferences=("城市漫步", "美食"),
        transport_preferences=("transit", "walking"),
        lodging_query=lodging_query,
        remarks="步行与公共交通结合；希望每天有灵活休息时间。",
    )
    requirements = RequirementSummary(
        requirements_id="requirements-f06",
        request_id=request.request_id,
        core_goals=("城市漫步", "美食"),
    )
    rules = RuleSet(
        rule_version="rules-f06-v1",
        windows=WINDOWS,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=requirements,
        rules=rules,
    ))


def make_facts(*, lodging_name="外滩酒店", sightseeing_city="上海"):
    source = FactSource(
        tool_call_id="source-synthetic-map",
        provider="amap",
        tool_name="maps_text_search",
        api_version="v4",
        queried_at=datetime.fromisoformat("2026-10-01T10:00:00+08:00"),
        effective_params={"city": "上海"},
    )
    places = (
        PlaceFact(
            fact_id="fact-bund",
            poi_id="poi-bund",
            name="外滩观景点",
            location=MapCoordinate(lng=121.49, lat=31.24),
            address="上海市黄浦区中山东一路",
            city=sightseeing_city,
            type_name="风景名胜",
            typecode="110000",
            source_refs=(source.tool_call_id,),
            field_sources={"name": source.tool_call_id, "location": source.tool_call_id},
        ),
        PlaceFact(
            fact_id="fact-meal",
            poi_id="poi-meal",
            name="合成餐厅",
            location=MapCoordinate(lng=121.50, lat=31.24),
            address="上海市黄浦区合成路1号",
            city="上海",
            type_name="餐饮服务",
            typecode="050000",
            source_refs=(source.tool_call_id,),
            field_sources={"name": source.tool_call_id},
        ),
        PlaceFact(
            fact_id="fact-hotel",
            poi_id="poi-hotel",
            name=lodging_name,
            location=MapCoordinate(lng=121.48, lat=31.23),
            address="上海市黄浦区外滩路1号",
            city="上海",
            type_name="住宿服务",
            typecode="100000",
            source_refs=(source.tool_call_id,),
            field_sources={"name": source.tool_call_id},
        ),
    )
    weather = WeatherFact(
        fact_id="fact-weather",
        city="上海",
        forecasts=(WeatherForecast(
            forecast_date=date(2026, 10, 10),
            day_weather="多云",
            day_temp_c=22,
            night_temp_c=17,
        ),),
        source=source,
    )
    return FactStore(sources=(source,), places=places, weather=(weather,))


def make_candidates(*, specified_lodging=False):
    return (
        CandidateRef(
            candidate_id="candidate-bund",
            category="sightseeing",
            poi_id="poi-bund",
            place_fact_id="fact-bund",
            slot_ids=("slot-2026-10-11-morning", "slot-2026-10-11-afternoon", "slot-2026-10-11-lunch"),
            requirement_ids=(),
            recommendation_reason="已核实的城市漫步地点。",
        ),
        # 同一 POI 可有不同候选 ID，不进行全局 POI 去重。
        CandidateRef(
            candidate_id="candidate-bund-alias",
            category="sightseeing",
            poi_id="poi-bund",
            place_fact_id="fact-bund",
            slot_ids=("slot-2026-10-11-morning",),
        ),
        CandidateRef(
            candidate_id="candidate-meal",
            category="meal",
            poi_id="poi-meal",
            place_fact_id="fact-meal",
            slot_ids=(),
        ),
        CandidateRef(
            candidate_id="candidate-hotel",
            category="lodging",
            poi_id="poi-hotel",
            place_fact_id="fact-hotel",
            area_hint_id=None if specified_lodging else "area-bund",
        ),
    )


def make_areas():
    return (
        AreaHint(
            area_hint_id="area-bund",
            label="外滩区域",
            anchor_poi_ids=("poi-bund",),
            basis_fact_ids=("fact-bund",),
        ),
        AreaHint(
            area_hint_id="area-city-center",
            label="市中心区域",
            anchor_poi_ids=("poi-meal",),
            basis_fact_ids=("fact-meal",),
        ),
    )


def make_model_output(planning_input, *, activities=None, lodging=True, weather=True, omit_one_unfilled=False):
    activities = activities or {}
    days = []
    for constraint in planning_input.days:
        day_activities = activities.get(constraint.day_id, ())
        used_slots = {activity["slot_id"] for activity in day_activities}
        unfilled = [
            slot.slot_id for slot in constraint.slots
            if slot.eligibility != "skipped" and slot.slot_id not in used_slots
        ]
        if omit_one_unfilled and constraint.day_id.endswith("2026-10-11"):
            if "slot-2026-10-11-evening" in unfilled:
                unfilled.remove("slot-2026-10-11-evening")
        days.append({
            "day_id": constraint.day_id,
            "day_date": constraint.day_date.isoformat(),
            "theme": f"第{constraint.day_date.day}日城市漫步",
            "activities": list(day_activities),
            "unfilled_slot_ids": unfilled,
            "lodging": (
                {"candidate_ref": "candidate-hotel", "area_hint_id": "area-bund"}
                if lodging and not planning_input.request.lodging_query else None
            ),
            "weather_advice": (
                "按当天预报准备轻便外套。" if weather and constraint.day_date == date(2026, 10, 10)
                else "这一天阳光充足，适合户外活动。" if weather else None
            ),
        })
    return json.dumps({"days": days, "missing_notes": ["尚未核实返程地点。"]}, ensure_ascii=False)


def make_generator(output):
    fake = FakeLLM([output])
    return DailyPlanGenerator(llm=fake), fake


class DailyPlanGeneratorTests(unittest.TestCase):
    def test_preallocated_plan_id_is_bound_to_generated_draft(self):
        planning_input = make_input()
        generator, fake = make_generator(make_model_output(planning_input))

        execution = generator.generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (),
            source_run_id=RUN_ID, plan_id="plan_preallocated-preview-id",
        )

        self.assertEqual(execution.draft.plan_id, "plan_preallocated-preview-id")
        self.assertEqual(execution.draft.source_run_id, RUN_ID)
        self.assertEqual(execution.proposal.request_id, planning_input.request.request_id)
        self.assertEqual(len(fake.calls), 1)

    def test_three_days_repeat_poi_order_area_and_weather_without_duration(self):
        planning_input = make_input()
        candidates = make_candidates()
        facts = make_facts()
        areas = make_areas()
        source_snapshot = planning_input.model_dump(mode="json")
        second = planning_input.days[1].day_id
        activities = {
            second: (
                {
                    "proposal_ref": "bund-morning",
                    "candidate_ref": "candidate-bund",
                    "slot_id": "slot-2026-10-11-morning",
                    "kind": "sightseeing",
                },
                {
                    "proposal_ref": "bund-afternoon",
                    "candidate_ref": "candidate-bund",
                    "slot_id": "slot-2026-10-11-afternoon",
                    "kind": "sightseeing",
                },
                {
                    "proposal_ref": "meal-lunch",
                    "candidate_ref": "candidate-meal",
                    "slot_id": "slot-2026-10-11-lunch",
                    "kind": "meal",
                },
                {
                    "proposal_ref": "free-evening",
                    "slot_id": "slot-2026-10-11-evening",
                    "kind": "free_time",
                },
            ),
        }
        raw = make_model_output(planning_input, activities=activities, omit_one_unfilled=True)
        generator, fake = make_generator(raw)

        execution = generator.generate(
            planning_input, candidates, facts, areas, (), source_run_id=RUN_ID,
        )

        # F06 的 free_time 默认占位也必须能进入 F09 和真实预览校验。
        report = validate_daily_plan(planning_input, execution.draft, facts, candidates)
        self.assertTrue(any(issue.code == "missing_sightseeing" for issue in report.issues))

        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(execution.model_calls, 1)
        self.assertEqual(execution.format_repairs, 0)
        self.assertEqual(execution.usage["total_tokens"], 180)
        self.assertEqual(execution.proposal.request_id, planning_input.request.request_id)
        self.assertEqual(execution.proposal.rule_version, planning_input.rules.rule_version)
        self.assertEqual(execution.proposal.area_hints, areas)
        self.assertEqual(planning_input.model_dump(mode="json"), source_snapshot)
        self.assertEqual(execution.draft.rule_version, planning_input.rules.rule_version)
        self.assertEqual([day.day_id for day in execution.draft.days], [day.day_id for day in planning_input.days])
        payload = json.loads(fake.calls[0][0][1]["content"])
        self.assertNotIn("slots", payload["planning_input"]["days"][0])
        self.assertIn(
            "slot-2026-10-11-morning",
            payload["day_slot_options"][1]["available_sightseeing_slot_ids"],
        )
        self.assertEqual(
            payload["candidates"][0]["applicable_slot_ids"],
            [slot.slot_id for day in planning_input.days for slot in day.slots
             if slot.eligibility != "skipped" and slot.period in {"morning", "afternoon", "evening"}],
        )
        self.assertEqual(len(payload["weather_facts"]), 1)

        day1, day2, day3 = execution.draft.days
        self.assertEqual(day1.weather_fact_id, "fact-weather")
        self.assertEqual(day1.weather_advice, "按当天预报准备轻便外套。")
        self.assertIsNone(day2.weather_fact_id)
        self.assertEqual(day2.weather_advice, "该日期暂无预报，天气建议未知。")
        self.assertIsNone(day3.weather_fact_id)
        self.assertEqual(day3.weather_advice, "该日期暂无预报，天气建议未知。")
        self.assertIn("slot-2026-10-11-evening", day2.unfilled_slot_ids)
        self.assertEqual(len(day2.activities), 4)
        first, second_visit, meal, free_time = day2.activities
        self.assertEqual(first.poi_id, second_visit.poi_id)
        self.assertNotEqual(first.activity_id, second_visit.activity_id)
        self.assertFalse({"duration_s", "duration_source", "duration_reason"} & first.model_dump().keys())
        self.assertEqual(first.activity_source, "model")
        self.assertEqual(second_visit.activity_source, "model")
        self.assertEqual(meal.activity_source, "model")
        self.assertEqual(free_time.activity_source, "model")
        self.assertEqual(day1.lodging.selection_source, "recommended")
        self.assertEqual(day1.lodging.area_hint_id, "area-bund")

    def test_backend_fills_omitted_unfilled_slots_across_all_three_days(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activities = {
            day_id: (
                {
                    "proposal_ref": "filled-morning",
                    "candidate_ref": "candidate-bund",
                    "slot_id": "slot-2026-10-11-morning",
                    "kind": "sightseeing",
                },
                {
                    "proposal_ref": "free-afternoon",
                    "slot_id": "slot-2026-10-11-afternoon",
                    "kind": "free_time",
                },
            ),
        }
        raw = json.loads(make_model_output(planning_input, activities=activities))
        for day in raw["days"]:
            day["unfilled_slot_ids"] = []
        generator, _ = make_generator(json.dumps(raw, ensure_ascii=False))

        execution = generator.generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        self.assertEqual(len(execution.draft.days), 3)
        for draft_day, constraint in zip(execution.draft.days, planning_input.days):
            covered = {
                "slot-2026-10-11-morning"
            } if draft_day.day_id == day_id else set()
            expected = tuple(
                slot.slot_id for slot in constraint.slots
                if slot.eligibility != "skipped" and slot.slot_id not in covered
            )
            self.assertEqual(draft_day.unfilled_slot_ids, expected)
        self.assertIn("slot-2026-10-11-afternoon", execution.draft.days[1].unfilled_slot_ids)

    def test_empty_verified_facts_produce_unfilled_draft_without_poi(self):
        planning_input = make_input()
        raw = make_model_output(planning_input, lodging=False)
        generator, _ = make_generator(raw)

        execution = generator.generate(
            planning_input, (), FactStore(), (), (), source_run_id=RUN_ID,
        )

        self.assertEqual(len(execution.draft.days), 3)
        self.assertTrue(all(not day.activities for day in execution.draft.days))
        self.assertTrue(all(
            activity.poi_id is None
            for day in execution.draft.days
            for activity in day.activities
        ))
        for draft_day, constraint in zip(execution.draft.days, planning_input.days):
            expected = tuple(
                slot.slot_id for slot in constraint.slots if slot.eligibility != "skipped"
            )
            self.assertEqual(draft_day.unfilled_slot_ids, expected)

    def test_invalid_or_duplicate_explicit_unfilled_reference_is_rejected(self):
        planning_input = make_input()
        cross_day_slot = "slot-2026-10-10-afternoon"
        skipped_slot = "slot-2026-10-10-breakfast"
        valid_day_slot = "slot-2026-10-11-breakfast"
        cases = (
            ("unknown", 1, ["slot-unknown"], "invalid_unfilled_slot"),
            ("cross-day", 1, [cross_day_slot], "invalid_unfilled_slot"),
            ("skipped", 0, [skipped_slot], "invalid_unfilled_slot"),
            ("duplicate", 1, [valid_day_slot, valid_day_slot], "duplicate_unfilled_slot"),
        )
        for label, day_index, slot_ids, expected_code in cases:
            with self.subTest(label=label):
                raw = json.loads(make_model_output(planning_input))
                raw["days"][day_index]["unfilled_slot_ids"] = slot_ids
                generator, _ = make_generator(json.dumps(raw, ensure_ascii=False))
                with self.assertRaises(PlanningError) as raised:
                    generator.generate(
                        planning_input,
                        make_candidates(),
                        make_facts(),
                        make_areas(),
                        (),
                        source_run_id=RUN_ID,
                    )
                self.assertEqual(raised.exception.code, "PLAN_REFERENCE_INVALID")
                self.assertIn(expected_code, raised.exception.reference_issue_codes)

    def test_duplicate_or_empty_weather_forecasts_are_unknown_and_hidden_from_model(self):
        planning_input = make_input()
        facts = make_facts()
        forecast_date = date(2026, 10, 10)
        valid = facts.weather[0].forecasts[0]
        cases = (
            ("duplicate", (valid, valid.model_copy(update={"day_weather": "雨"}))),
            ("empty", (WeatherForecast(forecast_date=forecast_date),)),
        )
        for label, forecasts in cases:
            with self.subTest(label=label):
                weather_fact = facts.weather[0].model_copy(update={"forecasts": forecasts})
                malformed_facts = facts.model_copy(update={"weather": (weather_fact,)})
                fake = FakeLLM([make_model_output(planning_input, weather=True)])
                execution = DailyPlanGenerator(llm=fake).generate(
                    planning_input,
                    make_candidates(),
                    malformed_facts,
                    make_areas(),
                    (),
                    source_run_id=RUN_ID,
                )

                self.assertIsNone(execution.draft.days[0].weather_fact_id)
                self.assertEqual(execution.draft.days[0].weather_advice, "该日期暂无预报，天气建议未知。")
                payload = json.loads(fake.calls[0][0][1]["content"])
                self.assertEqual(payload["weather_facts"], [])

    def test_valid_weather_without_model_advice_keeps_fact_and_uses_missing_advice_note(self):
        planning_input = make_input()
        for label, advice in (("missing", None), ("blank", " \t ")):
            with self.subTest(label=label):
                raw = json.loads(make_model_output(planning_input, weather=False))
                raw["days"][0]["weather_advice"] = advice
                fake = FakeLLM([json.dumps(raw, ensure_ascii=False)])

                execution = DailyPlanGenerator(llm=fake).generate(
                    planning_input,
                    make_candidates(),
                    make_facts(),
                    make_areas(),
                    (),
                    source_run_id=RUN_ID,
                )

                day = execution.draft.days[0]
                self.assertEqual(day.weather_fact_id, "fact-weather")
                self.assertEqual(
                    day.weather_advice,
                    "天气已查询，出行建议暂未生成，请结合当天天气调整。",
                )
                self.assertEqual(execution.model_calls, 1)
                self.assertEqual(execution.format_repairs, 0)

    def test_repairs_sightseeing_in_lunch_even_when_candidate_slot_allows_it(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "bad-period",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "sightseeing",
        }
        first_response = make_model_output(
            planning_input,
            activities={day_id: (activity,)},
        )
        repair = json.dumps({"repairs": [{
            "repair_id": "repair-0",
            "action": "replace",
            "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
        }]})
        fake = FakeLLM([first_response, repair])
        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        lunch = next(
            item for item in execution.draft.days[1].activities
            if item.slot_id == "slot-2026-10-11-lunch"
        )
        self.assertEqual(lunch.kind, "meal")
        self.assertEqual(lunch.candidate_ref, "candidate-meal")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(execution.model_calls, 2)
        self.assertEqual(execution.format_repairs, 1)
        self.assertEqual(execution.recovery_notices, ())
        repair_prompt = json.loads(fake.calls[1][0][-1]["content"])
        self.assertIn("活动kind不符合当前时段", repair_prompt["repair_targets"][0]["condition"])
        self.assertEqual(repair_prompt["repair_targets"][0]["required_kind"], "meal")

    def test_repairs_meal_in_morning(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "bad-meal-period",
            "candidate_ref": "candidate-meal",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "meal",
        }
        first_response = make_model_output(planning_input, activities={day_id: (activity,)})
        repair = json.dumps({"repairs": [{
            "repair_id": "repair-0",
            "action": "replace",
            "replacement": {"candidate_ref": "candidate-bund", "kind": "sightseeing"},
        }]})
        fake = FakeLLM([first_response, repair])
        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        morning = next(
            item for item in execution.draft.days[1].activities
            if item.slot_id == "slot-2026-10-11-morning"
        )
        self.assertEqual(morning.kind, "sightseeing")
        self.assertEqual(morning.candidate_ref, "candidate-bund")
        self.assertEqual(len(fake.calls), 2)

    def test_required_candidate_without_slot_scope_uses_any_available_sightseeing_slot(self):
        base_input = make_input()
        request = base_input.request.model_copy(update={"remarks": "必须参观外滩观景点。"})
        requirement = HardRequirement(
            requirement_id="must-visit-bund",
            kind="must_visit",
            text="必须参观外滩观景点",
            source=RequirementSource(field="remarks", quote="外滩观景点"),
        )
        requirements = base_input.requirements.model_copy(update={
            "request_id": request.request_id,
            "hard_requirements": (requirement,),
        })
        planning_input = PlanningInput(
            request=request,
            requirements=requirements,
            rules=base_input.rules,
            days=base_input.days,
            boundary_facts=base_input.boundary_facts,
        )
        candidates = make_candidates()
        required_candidate = candidates[0].model_copy(update={
            "slot_ids": (),
            "requirement_ids": ("must-visit-bund",),
        })
        candidates = (required_candidate, *candidates[1:])
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "must-visit-bund",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
            "requirement_ids": ["must-visit-bund"],
        }
        generator, fake = make_generator(make_model_output(planning_input, activities={day_id: (activity,)}))
        task_result = TaskResult(
            task_id="task-must-visit",
            run_id=RUN_ID,
            context_revision=1,
            request_id=request.request_id,
            status="complete",
            stop_reason="target_met",
            candidates=(required_candidate,),
        )
        execution = generator.generate(
            planning_input,
            candidates,
            make_facts(),
            make_areas(),
            (task_result,),
            source_run_id=RUN_ID,
        )
        selected = next(activity for activity in execution.draft.days[1].activities if activity.kind == "sightseeing")
        self.assertEqual(selected.candidate_ref, "candidate-bund")
        self.assertIn("slot-2026-10-11-morning", json.loads(fake.calls[0][0][1]["content"])["candidates"][0]["applicable_slot_ids"])

    def test_candidate_category_must_match_verified_place_type(self):
        planning_input = make_input()
        candidates = make_candidates()
        mislabeled = candidates[0].model_copy(update={"category": "meal"})
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "mislabeled-category",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "meal",
        }
        first_response = make_model_output(planning_input, activities={day_id: (activity,)})
        repair = json.dumps({"repairs": [{
            "repair_id": "repair-0",
            "action": "replace",
            "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
        }]})
        fake = FakeLLM([first_response, repair])
        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input,
            (mislabeled, *candidates[1:]),
            make_facts(),
            make_areas(),
            (),
            source_run_id=RUN_ID,
        )
        selected = next(
            item for item in execution.draft.days[1].activities
            if item.slot_id == "slot-2026-10-11-lunch"
        )
        self.assertEqual(selected.candidate_ref, "candidate-meal")
        self.assertEqual(selected.kind, "meal")
        self.assertEqual(len(fake.calls), 2)

    def test_repairs_incompatible_category_and_falls_back_for_foreign_city(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        category_bad = {
            "proposal_ref": "bad-category",
            "candidate_ref": "candidate-meal",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "sightseeing",
        }
        category_response = make_model_output(planning_input, activities={day_id: (category_bad,)})
        category_repair = json.dumps({"repairs": [{
            "repair_id": "repair-0",
            "action": "replace",
            "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
        }]})
        category_fake = FakeLLM([category_response, category_repair])
        generator = DailyPlanGenerator(llm=category_fake)
        category_execution = generator.generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        corrected = next(
            item for item in category_execution.draft.days[1].activities
            if item.slot_id == "slot-2026-10-11-lunch"
        )
        self.assertEqual(corrected.candidate_ref, "candidate-meal")
        self.assertEqual(corrected.kind, "meal")
        self.assertEqual(len(category_fake.calls), 2)
        category_prompt = json.loads(category_fake.calls[1][0][-1]["content"])
        self.assertIn("候选类别与required_kind不一致", category_prompt["repair_targets"][0]["condition"])
        self.assertEqual(category_prompt["repair_targets"][0]["required_kind"], "meal")

        city_bad = {
            "proposal_ref": "bad-city",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
        }
        generator, city_fake = make_generator(make_model_output(planning_input, activities={day_id: (city_bad,)}))
        city_execution = generator.generate(
            planning_input,
            make_candidates(),
            make_facts(sightseeing_city="杭州"),
            make_areas(),
            (),
            source_run_id=RUN_ID,
        )
        self.assertEqual(city_execution.draft.days[1].activities, ())
        self.assertIn("slot-2026-10-11-morning", city_execution.draft.days[1].unfilled_slot_ids)
        self.assertEqual(city_execution.recovery_notices[0].reason_code, "candidate_incompatible")
        self.assertEqual(len(city_fake.calls), 1)

    def test_local_patch_feedback_is_specific_and_preserves_locked_activities(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        payload = json.loads(make_model_output(planning_input, activities={day_id: (
            {
                "proposal_ref": "good-morning",
                "candidate_ref": "candidate-bund",
                "slot_id": "slot-2026-10-11-morning",
                "kind": "sightseeing",
            },
            {
                "proposal_ref": "unknown-candidate-activity",
                "candidate_ref": "unknown_candidate_secret_7913",
                "slot_id": "slot-2026-10-11-evening",
                "kind": "sightseeing",
            },
            {
                "proposal_ref": "invalid-structure-activity",
                "candidate_ref": "candidate-bund",
                "slot_id": "slot-2026-10-11-afternoon",
                "kind": "sightseeing",
                "private_unknown_field_2648": "sensitive_value_5821",
            },
            {
                "proposal_ref": "missing-lunch-candidate",
                "candidate_ref": None,
                "slot_id": "slot-2026-10-11-lunch",
                "kind": "meal",
            },
            {
                "proposal_ref": "good-afternoon",
                "candidate_ref": "candidate-bund",
                "slot_id": "slot-2026-10-11-afternoon",
                "kind": "sightseeing",
            },
        )}))
        response = "```json\n" + json.dumps({
            # A patch response cannot replace dates, themes, lodging or other activities.
            "days": [],
            "repairs": [
                {
                    "repair_id": "repair-0",
                    "action": "replace",
                    "replacement": {"candidate_ref": "candidate-bund-evening", "kind": "sightseeing"},
                },
                {
                    "repair_id": "repair-1",
                    "action": "replace",
                    "replacement": {"candidate_ref": "candidate-bund", "kind": "sightseeing"},
                },
                {
                    "repair_id": "repair-2",
                    "action": "replace",
                    "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
                },
            ],
        }) + "\n```"
        candidates = (
            *make_candidates(),
            make_candidates()[0].model_copy(update={
                "candidate_id": "candidate-bund-evening",
                "slot_ids": ("slot-2026-10-11-evening",),
            }),
        )
        fake = FakeLLM([json.dumps(payload, ensure_ascii=False), response])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, candidates, make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        patch_payload = json.loads(fake.calls[1][0][-1]["content"])
        targets = patch_payload["repair_targets"]
        self.assertEqual(len(targets), 3)
        self.assertEqual(
            {(target["day_date"], target["slot_id"], target["required_kind"]) for target in targets},
            {
                ("2026-10-11", "slot-2026-10-11-evening", "sightseeing"),
                ("2026-10-11", "slot-2026-10-11-afternoon", "sightseeing"),
                ("2026-10-11", "slot-2026-10-11-lunch", "meal"),
            },
        )
        feedback = json.dumps(patch_payload, ensure_ascii=False)
        self.assertIn("candidate_ref 不在当前已核实候选中", feedback)
        self.assertIn("地点活动必须引用一个当前有效候选", feedback)
        self.assertIn("活动字段结构不符合契约", feedback)
        self.assertIn("candidate-bund-evening", feedback)
        self.assertIn('"candidate_id": "candidate-bund"', feedback)
        self.assertIn("candidate-meal", feedback)
        self.assertNotIn("unknown_candidate_secret_7913", feedback)
        self.assertNotIn("private_unknown_field_2648", feedback)
        self.assertNotIn("sensitive_value_5821", feedback)

        day = execution.draft.days[1]
        self.assertEqual(
            [(item.slot_id, item.candidate_ref) for item in day.activities],
            [
                ("slot-2026-10-11-morning", "candidate-bund"),
                ("slot-2026-10-11-evening", "candidate-bund-evening"),
                ("slot-2026-10-11-afternoon", "candidate-bund"),
                ("slot-2026-10-11-lunch", "candidate-meal"),
                ("slot-2026-10-11-afternoon", "candidate-bund"),
            ],
        )
        self.assertEqual(day.theme, "第11日城市漫步")
        self.assertEqual(len({item.activity_id for item in day.activities}), 5)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(execution.format_repairs, 1)
        self.assertEqual(execution.recovery_notices, ())

    def test_unrepaired_local_activity_becomes_unfilled_without_losing_valid_activity(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        payload = json.loads(make_model_output(planning_input, activities={day_id: (
            {
                "proposal_ref": "good-morning",
                "candidate_ref": "candidate-bund",
                "slot_id": "slot-2026-10-11-morning",
                "kind": "sightseeing",
            },
            {
                "proposal_ref": "bad-morning",
                "candidate_ref": "unknown_candidate_secret_7913",
                "slot_id": "slot-2026-10-11-morning",
                "kind": "sightseeing",
            },
        )}))
        fake = FakeLLM([json.dumps(payload), "not a repair payload"])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        day = execution.draft.days[1]
        self.assertEqual(len(day.activities), 1)
        self.assertEqual(day.activities[0].candidate_ref, "candidate-bund")
        # The slot remains covered by a valid visit, but F06 still reports the rejected
        # original activity through the slot-scoped recovery notice.
        self.assertNotIn("slot-2026-10-11-morning", day.unfilled_slot_ids)
        self.assertEqual(len(execution.recovery_notices), 1)
        self.assertEqual(execution.recovery_notices[0].slot_id, "slot-2026-10-11-morning")
        self.assertEqual(execution.recovery_notices[0].reason_code, "candidate_reference_invalid")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(execution.model_calls, 2)

    def test_no_selectable_replacement_marks_slot_unfilled_without_extra_call(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        payload = json.loads(make_model_output(planning_input, activities={day_id: (
            {
                "proposal_ref": "bad-lunch",
                "candidate_ref": None,
                "slot_id": "slot-2026-10-11-lunch",
                "kind": "meal",
            },
        )}))
        candidates = tuple(item for item in make_candidates() if item.candidate_id != "candidate-meal")
        fake = FakeLLM([json.dumps(payload)])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, candidates, make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        self.assertEqual(execution.draft.days[1].activities, ())
        self.assertIn("slot-2026-10-11-lunch", execution.draft.days[1].unfilled_slot_ids)
        self.assertEqual(len(execution.recovery_notices), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(execution.model_calls, 1)
        self.assertEqual(execution.usage["total_tokens"], 180)

    def test_unlocatable_day_identity_error_is_not_downgraded(self):
        planning_input = make_input()
        payload = json.loads(make_model_output(planning_input))
        payload["days"][1]["day_id"] = "foreign-day-identity"
        fake = FakeLLM([json.dumps(payload)])

        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )

        self.assertEqual(raised.exception.code, "PLAN_REFERENCE_INVALID")
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_requirement_is_repaired_with_only_allowed_ids(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        payload = json.loads(make_model_output(planning_input, activities={day_id: ({
            "proposal_ref": "unknown-requirement",
            "candidate_ref": "candidate-meal",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "meal",
            "requirement_ids": ["private_unknown_requirement_437"],
        },)}))
        patch_response = json.dumps({"repairs": [{
            "repair_id": "repair-0",
            "action": "replace",
            "replacement": {"candidate_ref": "candidate-meal", "kind": "meal", "requirement_ids": []},
        }]})
        fake = FakeLLM([json.dumps(payload), patch_response])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        repair_prompt = json.loads(fake.calls[1][0][-1]["content"])
        target = repair_prompt["repair_targets"][0]
        self.assertIn("requirement_ids含未知需求", target["condition"])
        self.assertEqual(target["available_verified_candidates"][0]["allowed_requirement_ids"], [])
        self.assertNotIn("private_unknown_requirement_437", json.dumps(repair_prompt))
        lunch = next(
            item for item in execution.draft.days[1].activities
            if item.slot_id == "slot-2026-10-11-lunch"
        )
        self.assertEqual(lunch.candidate_ref, "candidate-meal")
        self.assertEqual(lunch.requirement_ids, ())
        self.assertEqual(len(fake.calls), 2)

    def test_repair_budget_from_truncated_output_allows_safe_unfilled_fallback(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        initial_valid = make_model_output(planning_input)
        bad_second_response = make_model_output(planning_input, activities={day_id: ({
            "proposal_ref": "missing-lunch-candidate",
            "candidate_ref": None,
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "meal",
        },)})
        fake = MetadataFakeLLM([(initial_valid, "length"), (bad_second_response, "stop")])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(execution.model_calls, 2)
        self.assertEqual(execution.format_repairs, 1)
        self.assertIn("slot-2026-10-11-lunch", execution.draft.days[1].unfilled_slot_ids)
        self.assertEqual(execution.recovery_notices[0].reason_code, "activity_kind_reference_mismatch")

    def test_duplicate_repair_id_with_one_bad_item_is_ambiguous(self):
        response = json.dumps({"repairs": [
            {
                "repair_id": "repair-0",
                "action": "replace",
                "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
                "unexpected_field": "ignored but ambiguous",
            },
            {
                "repair_id": "repair-0",
                "action": "replace",
                "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
            },
        ]})

        self.assertEqual(parse_patch_response(response, {"repair-0"}), {})

    def test_local_patch_accepts_only_complete_json_fences(self):
        patch = {
            "repairs": [{
                "repair_id": "repair-0",
                "action": "replace",
                "replacement": {"candidate_ref": "candidate-meal", "kind": "meal"},
            }],
        }
        body = json.dumps(patch)
        target_ids = {"repair-0"}

        self.assertIn("repair-0", parse_patch_response(f"```json\n{body}\n```", target_ids))
        self.assertEqual(parse_patch_response(f"```json\n{body}", target_ids), {})
        self.assertEqual(parse_patch_response(f"```json\n{body}\n```\nextra", target_ids), {})
        self.assertEqual(parse_patch_response(f"```python\n{body}\n```", target_ids), {})

    def test_local_repair_observation_records_safe_target_for_success_and_call_failure(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        bad_output = make_model_output(planning_input, activities={day_id: ({
            "proposal_ref": "untrusted-model-ref",
            "candidate_ref": "unknown_candidate_secret_7913",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
        },)})
        outcomes = (
            ("success", json.dumps({"repairs": [{
                "repair_id": "repair-0",
                "action": "replace",
                "replacement": {"candidate_ref": "candidate-bund", "kind": "sightseeing"},
            }]})),
            ("failure", RuntimeError("provider_secret_615")),
        )

        for label, repair_response in outcomes:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                task_id = f"local-repair-observation-{label}"
                repository = TaskRepository(Path(directory) / "observation.sqlite3")
                repository.initialize()
                repository.create(task_id, {})
                fake = FakeLLM([bad_output, repair_response])
                with ObservationService(repository).task(task_id):
                    execution = DailyPlanGenerator(llm=fake).generate(
                        planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
                    )

                rows = repository.spans(task_id)
                recovery_rows = [row for row in rows if row["name"] == "validation.daily_plan_local_repair"]
                self.assertEqual(len(recovery_rows), 1)
                recovery = repository.span(task_id, recovery_rows[0]["span_id"])["output_data"]
                serialized = json.dumps([
                    repository.span(task_id, row["span_id"])
                    for row in rows
                ], ensure_ascii=False)
                self.assertEqual(recovery["target_count"], 1)
                self.assertFalse(recovery["diagnostics_truncated"])
                self.assertEqual(recovery["diagnostics"][0]["path"], "days[1].activities[0]")
                self.assertIn("unknown_candidate", recovery["diagnostics"][0]["reason_codes"])
                self.assertIn("candidate_ref 不在当前已核实候选中", recovery["diagnostics"][0]["guidance"])
                self.assertNotIn("unknown_candidate_secret_7913", serialized)
                self.assertNotIn("untrusted-model-ref", serialized)
                self.assertNotIn("provider_secret_615", serialized)
                self.assertEqual(len(fake.calls), 2)
                if label == "failure":
                    self.assertEqual(len(execution.recovery_notices), 1)
                    self.assertEqual(recovery["status"], "repair_call_unavailable")
                else:
                    self.assertEqual(execution.recovery_notices, ())
                    self.assertEqual(recovery["status"], "repaired")

    def test_cancellation_is_not_downgraded_when_local_repair_has_no_candidates(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        bad_output = make_model_output(planning_input, activities={day_id: ({
            "proposal_ref": "missing-lunch-candidate",
            "candidate_ref": None,
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "meal",
        },)})
        cancel_event = Event()

        class CancellingLLM:
            def __init__(self):
                self.calls = []

            def invoke_with_usage(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                cancel_event.set()
                return bad_output, {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180}

        fake = CancellingLLM()
        candidates = tuple(item for item in make_candidates() if item.candidate_id != "candidate-meal")
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, candidates, make_facts(), make_areas(), (),
                source_run_id=RUN_ID, cancel_event=cancel_event,
            )

        self.assertEqual(raised.exception.code, "MODEL_CALL_CANCELLED")
        self.assertEqual(len(fake.calls), 1)

    def test_cancellation_after_local_response_keeps_completed_call_statistics(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        bad_output = make_model_output(planning_input, activities={day_id: ({
            "proposal_ref": "bad-candidate",
            "candidate_ref": "unknown_candidate_7913",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
        },)})
        cancel_event = Event()
        fake = FakeLLM([bad_output])
        generator = DailyPlanGenerator(llm=fake)

        def completed_repair_then_cancel(snapshot, *args, **kwargs):
            cancel_event.set()
            return (
                snapshot.rebuild({}),
                (),
                1,
                {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                True,
            )

        with patch.object(generator, "_repair_local_activities", side_effect=completed_repair_then_cancel):
            with self.assertRaises(PlanningError) as raised:
                generator.generate(
                    planning_input, make_candidates(), make_facts(), make_areas(), (),
                    source_run_id=RUN_ID, cancel_event=cancel_event,
                )

        self.assertEqual(raised.exception.code, "MODEL_CALL_CANCELLED")
        self.assertEqual(raised.exception.model_calls, 2)
        self.assertEqual(raised.exception.format_repairs, 1)
        self.assertEqual(raised.exception.usage["total_tokens"], 192)

    def test_invalid_candidate_source_and_task_run_fail_before_model_call(self):
        planning_input = make_input()
        generator, fake = make_generator(make_model_output(planning_input))
        broken_candidate = make_candidates()[0].model_copy(update={"place_fact_id": "missing-fact"})
        with self.assertRaises(PlanningError) as candidate_error:
            generator.generate(
                planning_input,
                (broken_candidate, *make_candidates()[1:]),
                make_facts(),
                make_areas(),
                (),
                source_run_id=RUN_ID,
            )
        self.assertIn("candidate_fact_missing", candidate_error.exception.reference_issue_codes)
        self.assertEqual(candidate_error.exception.model_calls, 0)
        self.assertEqual(candidate_error.exception.format_repairs, 0)
        self.assertEqual(candidate_error.exception.usage["total_tokens"], 0)
        self.assertEqual(fake.calls, [])

        facts = make_facts()
        broken_place = facts.places[0].model_copy(update={"field_sources": {"name": "foreign-source"}})
        broken_facts = facts.model_copy(update={"places": (broken_place, *facts.places[1:])})
        with self.assertRaises(PlanningError) as source_error:
            generator.generate(
                planning_input, make_candidates(), broken_facts, make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertIn("dangling_place_field_source", source_error.exception.reference_issue_codes)
        self.assertEqual(fake.calls, [])

        from app.models.planning_v4 import TaskResult
        foreign_task = TaskResult(
            task_id="task-old",
            run_id="foreign-run",
            context_revision=1,
            request_id=planning_input.request.request_id,
            status="partial",
            stop_reason="no_progress",
        )
        with self.assertRaises(PlanningError) as task_error:
            generator.generate(
                planning_input,
                make_candidates(),
                make_facts(),
                make_areas(),
                (foreign_task,),
                source_run_id=RUN_ID,
            )
        self.assertIn("task_run_mismatch", task_error.exception.reference_issue_codes)
        self.assertEqual(fake.calls, [])

    def test_unverified_city_candidate_cannot_be_selected(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "missing-city",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
        }
        generator, fake = make_generator(make_model_output(planning_input, activities={day_id: (activity,)}))
        facts = make_facts(sightseeing_city=None)
        execution = generator.generate(
            planning_input, make_candidates(), facts, make_areas(), (), source_run_id=RUN_ID,
        )
        self.assertEqual(execution.draft.days[1].activities, ())
        self.assertIn("slot-2026-10-11-morning", execution.draft.days[1].unfilled_slot_ids)
        self.assertEqual(execution.recovery_notices[0].reason_code, "candidate_incompatible")
        self.assertEqual(len(fake.calls), 1)

    def test_specified_lodging_is_fixed_by_backend_or_unknown_without_fallback(self):
        planning_input = make_input(lodging_query="外滩酒店")
        candidates = make_candidates(specified_lodging=True)
        generator, _ = make_generator(make_model_output(planning_input, lodging=False))
        execution = generator.generate(
            planning_input, candidates, make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        self.assertEqual({day.lodging.selection_source for day in execution.draft.days}, {"user"})
        self.assertEqual({day.lodging.candidate_ref for day in execution.draft.days}, {"candidate-hotel"})
        self.assertTrue(all(day.lodging.area_hint_id is None for day in execution.draft.days))

        planning_input = make_input(lodging_query="另一家酒店")
        generator, _ = make_generator(make_model_output(planning_input, lodging=False))
        execution = generator.generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        self.assertEqual({day.lodging.selection_source for day in execution.draft.days}, {"unknown"})
        self.assertTrue(all(day.lodging.candidate_ref is None for day in execution.draft.days))

    def test_format_only_repair_is_limited_to_one_and_usage_is_aggregated(self):
        planning_input = make_input()
        valid = make_model_output(planning_input)
        malformed = json.dumps({"days": [], "area_hints": [{"center": {"lat": 0, "lng": 0}}]})
        fake = FakeLLM([malformed, valid])
        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        self.assertEqual(execution.model_calls, 2)
        self.assertEqual(execution.format_repairs, 1)
        self.assertEqual(execution.usage, {"input_tokens": 240, "output_tokens": 120, "total_tokens": 360})
        repair = fake.calls[1][0][-1]["content"]
        self.assertIn("只修复结构诊断中的问题", repair)
        self.assertNotIn("max_tokens", fake.calls[0][1])
        self.assertEqual(fake.calls[0][1]["max_retries"], 0)
        self.assertLessEqual(fake.calls[0][1]["request_timeout"], 120)

    def test_format_repair_keeps_safe_type_and_gives_fixed_too_short_guidance(self):
        planning_input = make_input()
        valid = make_model_output(planning_input)
        malformed = json.dumps({"days": [], "missing_notes": []})
        fake = FakeLLM([malformed, valid])

        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )

        repair = fake.calls[1][0][-1]["content"]
        self.assertEqual(execution.format_repairs, 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn('"type": "too_short"', repair)
        self.assertIn("保留所有请求日期", repair)

    def test_safe_format_diagnostics_hide_unknown_fields_and_values(self):
        planning_input = make_input()
        payload = json.loads(make_model_output(planning_input))
        day = payload["days"][1]
        day["activities"].append({
            "proposal_ref": "synthetic-invalid-free-time",
            "slot_id": "slot-2026-10-11-evening",
            "kind": "free_time",
            "candidate_ref": "sensitive_candidate_value_7913",
        })

        parsed, diagnostics, issue_count, truncated = DailyPlanGenerator._validate_model_output(
            json.dumps(payload, ensure_ascii=False), DailyPlanModelOutput,
        )

        rendered = json.dumps(diagnostics, ensure_ascii=False)
        self.assertIsNone(parsed)
        self.assertGreaterEqual(issue_count, 1)
        self.assertFalse(truncated)
        self.assertIn('"path": "days[1].activities[0]"', rendered)
        self.assertIn('"type": "value_error"', rendered)
        self.assertIn("sightseeing/meal必须引用候选", rendered)
        self.assertIn("free_time不得引用POI候选", rendered)
        self.assertNotIn("sensitive_candidate_value_7913", rendered)

        payload["days"][1]["activities"][0]["private_user_value_2648"] = "sensitive_field_value_5821"
        _, unknown_field_diagnostics, _, _ = DailyPlanGenerator._validate_model_output(
            json.dumps(payload, ensure_ascii=False), DailyPlanModelOutput,
        )
        unknown_field_rendered = json.dumps(unknown_field_diagnostics, ensure_ascii=False)
        self.assertIn('"path": "days[1].activities[0].unknown_field"', unknown_field_rendered)
        self.assertNotIn("private_user_value_2648", unknown_field_rendered)
        self.assertNotIn("sensitive_field_value_5821", unknown_field_rendered)

    def test_format_diagnostic_cap_keeps_actual_issue_count(self):
        payload = json.loads(make_model_output(make_input()))
        payload.update({f"unexpected_private_field_{index}": index for index in range(9)})

        parsed, diagnostics, issue_count, truncated = DailyPlanGenerator._validate_model_output(
            json.dumps(payload), DailyPlanModelOutput,
        )

        self.assertIsNone(parsed)
        self.assertEqual(issue_count, 9)
        self.assertTrue(truncated)
        self.assertEqual(len(diagnostics), 9)
        self.assertEqual(diagnostics[-1]["type"], "additional_issues")
        rendered = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn("unexpected_private_field", rendered)

    def test_observation_records_safe_output_diagnostics_but_not_model_response(self):
        planning_input = make_input()
        valid = make_model_output(planning_input)
        payload = json.loads(valid)
        payload["days"][1]["activities"].append({
            "proposal_ref": "synthetic-invalid-free-time",
            "slot_id": "slot-2026-10-11-evening",
            "kind": "free_time",
            "candidate_ref": "SYNTHETIC_CANDIDATE_SECRET_7913",
        })
        payload["private_user_field_2648"] = "SYNTHETIC_INPUT_VALUE_5821"
        malformed = json.dumps(payload, ensure_ascii=False)
        fake = FakeLLM([malformed, valid])

        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "daily-plan-observation.sqlite3")
            repository.initialize()
            repository.create("daily-plan-observation-task", {})
            with ObservationService(repository).task("daily-plan-observation-task"):
                execution = DailyPlanGenerator(llm=fake).generate(
                    planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
                )

            rows = repository.spans("daily-plan-observation-task")
            validation_rows = [row for row in rows if row["name"] == "validation.daily_plan_output"]
            self.assertEqual(len(validation_rows), 2)
            failed_validation = repository.span("daily-plan-observation-task", validation_rows[0]["span_id"])
            diagnostic_output = json.dumps(failed_validation["output_data"], ensure_ascii=False)
            all_observation_data = json.dumps([
                repository.span("daily-plan-observation-task", row["span_id"])
                for row in rows
            ], ensure_ascii=False)

        self.assertEqual(execution.format_repairs, 1)
        self.assertFalse(failed_validation["output_data"]["valid"])
        self.assertEqual(failed_validation["output_data"]["issue_count"], 2)
        self.assertFalse(failed_validation["output_data"]["diagnostics_truncated"])
        self.assertIn('"path": "days[1].activities[0]"', diagnostic_output)
        self.assertIn('"type": "value_error"', diagnostic_output)
        self.assertIn('"path": "unknown_field"', diagnostic_output)
        self.assertIn('"type": "extra_forbidden"', diagnostic_output)
        self.assertNotIn("private_user_field_2648", all_observation_data)
        self.assertNotIn("SYNTHETIC_INPUT_VALUE_5821", all_observation_data)
        self.assertNotIn("SYNTHETIC_CANDIDATE_SECRET_7913", all_observation_data)
        self.assertNotIn(malformed, all_observation_data)

    def test_cancellation_after_invalid_output_prevents_format_repair_request(self):
        planning_input = make_input()
        malformed = json.dumps({"days": []})
        cancel_event = Event()

        class CancellingLLM:
            def __init__(self):
                self.calls = []

            def invoke_with_usage(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                cancel_event.set()
                return malformed, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

        fake = CancellingLLM()
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (),
                source_run_id=RUN_ID, cancel_event=cancel_event,
            )

        self.assertEqual(raised.exception.code, "MODEL_CALL_CANCELLED")
        self.assertEqual(len(fake.calls), 1)

    def test_failed_format_repair_carries_cumulative_execution_stats(self):
        planning_input = make_input()
        malformed = json.dumps({"days": [], "area_hints": [{"center": {"lat": 0, "lng": 0}}]})
        fake = FakeLLM([malformed, malformed])
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )

        self.assertEqual(raised.exception.code, "PLAN_OUTPUT_INVALID")
        self.assertEqual(raised.exception.model_calls, 2)
        self.assertEqual(raised.exception.format_repairs, 1)
        self.assertEqual(raised.exception.usage, {
            "input_tokens": 240,
            "output_tokens": 120,
            "total_tokens": 360,
        })

    def test_provider_length_never_accepts_valid_looking_empty_plan(self):
        planning_input = make_input()
        valid_empty = make_model_output(planning_input)
        fake = MetadataFakeLLM([(valid_empty, "length"), (valid_empty, "length")])
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertEqual(raised.exception.code, "PLAN_OUTPUT_TRUNCATED")
        self.assertEqual(raised.exception.model_calls, 2)
        self.assertEqual(raised.exception.format_repairs, 1)
        self.assertEqual(raised.exception.usage["total_tokens"], 360)
        self.assertEqual(len(fake.calls), 2)

    def test_provider_length_allows_only_one_completion_repair(self):
        planning_input = make_input()
        valid = make_model_output(planning_input)
        fake = MetadataFakeLLM([(valid, "length"), (valid, "stop")])
        execution = DailyPlanGenerator(llm=fake).generate(
            planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
        )
        self.assertEqual(execution.format_repairs, 1)
        self.assertEqual(execution.model_calls, 2)
        self.assertIn("长度限制被截断", fake.calls[1][0][-1]["content"])

    def test_provider_content_filter_reason_is_a_safe_failure(self):
        planning_input = make_input()
        fake = MetadataFakeLLM([(make_model_output(planning_input), "content_filter")])
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertEqual(raised.exception.code, "PLAN_OUTPUT_INCOMPLETE")
        self.assertEqual(len(fake.calls), 1)

    def test_model_errors_are_sanitized(self):
        planning_input = make_input()
        fake = FakeLLM([RuntimeError("provider response leaked-sensitive-content")])
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertEqual(raised.exception.code, "MODEL_CALL_FAILED")
        self.assertNotIn("leaked-sensitive-content", str(raised.exception))
        self.assertEqual(getattr(raised.exception, "model_calls", None), 1)

    def test_started_request_timeout_reports_call_and_unknown_usage(self):
        planning_input = make_input()
        entered = Event()
        release = Event()
        valid = make_model_output(planning_input)

        class LateLLM:
            def invoke_with_usage(self, messages, **kwargs):
                entered.set()
                release.wait(timeout=1)
                return valid, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

        try:
            with self.assertRaises(PlanningError) as raised:
                DailyPlanGenerator(llm=LateLLM(), timeout_seconds=0.03).generate(
                    planning_input,
                    make_candidates(),
                    make_facts(),
                    make_areas(),
                    (),
                    source_run_id=RUN_ID,
                )
            self.assertTrue(entered.is_set())
            self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")
            self.assertEqual(raised.exception.model_calls, 1)
            self.assertEqual(raised.exception.format_repairs, 0)
            self.assertIsNone(raised.exception.usage)
        finally:
            release.set()

    def test_queued_timeout_reports_zero_calls_and_known_zero_usage(self):
        planning_input = make_input()

        class QueuedExecutor:
            def __init__(self):
                self.future = None

            def submit(self, *args, **kwargs):
                self.future = Future()
                return self.future

        executor = QueuedExecutor()
        fake = FakeLLM([])
        with patch("app.agents.daily_plan_generator._MODEL_EXECUTOR", executor):
            with self.assertRaises(PlanningError) as raised:
                DailyPlanGenerator(llm=fake, timeout_seconds=0.02).generate(
                    planning_input,
                    make_candidates(),
                    make_facts(),
                    make_areas(),
                    (),
                    source_run_id=RUN_ID,
                )

        self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")
        self.assertEqual(raised.exception.model_calls, 0)
        self.assertEqual(raised.exception.format_repairs, 0)
        self.assertEqual(raised.exception.usage, {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        })
        self.assertTrue(executor.future.cancelled())
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
