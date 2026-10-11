"""F06 每日安排生成：只用合成事实和替身模型进行离线测试。"""
from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime
import json
from threading import Event
import unittest
from unittest.mock import patch

from app.agents.daily_plan_generator import DailyPlanGenerator
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
    def test_three_days_repeat_poi_duration_defaults_area_and_weather(self):
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
                    "duration_s": 0,
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
            ["slot-2026-10-11-morning", "slot-2026-10-11-afternoon"],
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
        self.assertEqual(first.duration_s, 0)
        self.assertEqual(first.duration_source, "model")
        self.assertEqual(second_visit.duration_s, 3 * 60 * 60)
        self.assertEqual(second_visit.duration_source, "default")
        self.assertEqual(meal.duration_s, 60 * 60)
        self.assertEqual(meal.duration_source, "default")
        self.assertEqual(free_time.duration_s, 0)
        self.assertEqual(free_time.duration_source, "default")
        self.assertIn("不表示", free_time.duration_reason)
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

    def test_rejects_sightseeing_in_lunch_even_when_candidate_slot_allows_it(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "bad-period",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "sightseeing",
        }
        generator, fake = make_generator(make_model_output(
            planning_input,
            activities={day_id: (activity,)},
        ))
        with self.assertRaises(PlanningError) as raised:
            generator.generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertEqual(raised.exception.code, "PLAN_REFERENCE_INVALID")
        self.assertIn("activity_kind_slot_mismatch", raised.exception.reference_issue_codes)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(raised.exception.model_calls, 1)
        self.assertEqual(raised.exception.format_repairs, 0)
        self.assertEqual(raised.exception.usage["total_tokens"], 180)

    def test_rejects_meal_in_morning(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        activity = {
            "proposal_ref": "bad-meal-period",
            "candidate_ref": "candidate-meal",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "meal",
        }
        generator, fake = make_generator(make_model_output(planning_input, activities={day_id: (activity,)}))
        with self.assertRaises(PlanningError) as raised:
            generator.generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertIn("activity_kind_slot_mismatch", raised.exception.reference_issue_codes)
        self.assertEqual(len(fake.calls), 1)

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
        generator, fake = make_generator(make_model_output(planning_input, activities={day_id: (activity,)}))
        with self.assertRaises(PlanningError) as raised:
            generator.generate(
                planning_input, (mislabeled, *candidates[1:]), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertIn("selected_candidate_category_invalid", raised.exception.reference_issue_codes)
        self.assertEqual(len(fake.calls), 1)

    def test_rejects_category_and_foreign_city_selections(self):
        planning_input = make_input()
        day_id = planning_input.days[1].day_id
        category_bad = {
            "proposal_ref": "bad-category",
            "candidate_ref": "candidate-meal",
            "slot_id": "slot-2026-10-11-lunch",
            "kind": "sightseeing",
        }
        generator, _ = make_generator(make_model_output(planning_input, activities={day_id: (category_bad,)}))
        with self.assertRaises(PlanningError) as category_error:
            generator.generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertIn("candidate_category_mismatch", category_error.exception.reference_issue_codes)

        city_bad = {
            "proposal_ref": "bad-city",
            "candidate_ref": "candidate-bund",
            "slot_id": "slot-2026-10-11-morning",
            "kind": "sightseeing",
        }
        generator, _ = make_generator(make_model_output(planning_input, activities={day_id: (city_bad,)}))
        with self.assertRaises(PlanningError) as city_error:
            generator.generate(
                planning_input,
                make_candidates(),
                make_facts(sightseeing_city="杭州"),
                make_areas(),
                (),
                source_run_id=RUN_ID,
            )
        self.assertIn("selected_candidate_city_invalid", city_error.exception.reference_issue_codes)

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
        generator, _ = make_generator(make_model_output(planning_input, activities={day_id: (activity,)}))
        facts = make_facts(sightseeing_city=None)
        with self.assertRaises(PlanningError) as raised:
            generator.generate(
                planning_input, make_candidates(), facts, make_areas(), (), source_run_id=RUN_ID,
            )
        self.assertIn("selected_candidate_city_invalid", raised.exception.reference_issue_codes)

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
        self.assertIn("只修复格式和结构", repair)
        self.assertEqual(fake.calls[0][1]["max_tokens"], 8192)
        self.assertEqual(fake.calls[0][1]["max_retries"], 0)
        self.assertLessEqual(fake.calls[0][1]["request_timeout"], 120)

    def test_failed_format_repair_carries_cumulative_execution_stats(self):
        planning_input = make_input()
        malformed = json.dumps({"days": [], "area_hints": [{"center": {"lat": 0, "lng": 0}}]})
        fake = FakeLLM([malformed, malformed])
        with self.assertRaises(PlanningError) as raised:
            DailyPlanGenerator(llm=fake).generate(
                planning_input, make_candidates(), make_facts(), make_areas(), (), source_run_id=RUN_ID,
            )

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
