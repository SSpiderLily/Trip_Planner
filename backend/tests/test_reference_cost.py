"""规划 v4 已知人均消费汇总的离线回归。"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from app.models.planning_v4 import (
    DayConstraint,
    DraftActivity,
    DraftDay,
    FactSource,
    FactStore,
    LodgingRef,
    PlaceFact,
    PlanningInput,
    ReferenceCost,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)
from app.models.planning_v4.plan import PlanDraft
from app.services.reference_cost import (
    ReferenceCostAggregationError,
    aggregate_reference_costs,
)


TZ = "Asia/Shanghai"
START = date(2026, 10, 10)
DAYS = tuple(START + timedelta(days=index) for index in range(3))
PERIODS = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")


def make_planning_input() -> PlanningInput:
    request = TripRequest(
        request_id="cost-shanghai-3d",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T20:00:00+08:00"),
        timezone=TZ,
    )
    requirements = RequirementSummary(
        requirements_id="cost-requirements",
        request_id=request.request_id,
    )
    rules = RuleSet(
        rule_version="cost-rules-1",
        windows=(
            {"period": "breakfast", "start_minute": 420, "end_minute": 600},
            {"period": "morning", "start_minute": 600, "end_minute": 720},
            {"period": "lunch", "start_minute": 720, "end_minute": 840},
            {"period": "afternoon", "start_minute": 840, "end_minute": 1080},
            {"period": "dinner", "start_minute": 1080, "end_minute": 1200},
            {"period": "evening", "start_minute": 1200, "end_minute": 1380},
        ),
    )
    days = tuple(
        DayConstraint(
            day_id=f"day-{index + 1}",
            day_date=day_date,
            slots=tuple(
                SlotConstraint(
                    slot_id=f"slot-{index + 1}-{period}",
                    day_id=f"day-{index + 1}",
                    period=period,
                    eligibility="required",
                )
                for period in PERIODS
            ),
        )
        for index, day_date in enumerate(DAYS)
    )
    return PlanningInput(request=request, requirements=requirements, rules=rules, days=days)


def make_facts() -> FactStore:
    queried_at = datetime.fromisoformat("2026-10-10T08:05:00+08:00")
    fact_specs = (
        ("bund-day1", "poi-bund", "外滩", "22.50", "22.50 元"),
        ("bund-day2", "poi-bund", "外滩", "22.50", "22.50 元"),
        ("bund-unselected-old", "poi-bund", "外滩", "999", "999 元"),
        ("museum-high", "poi-museum", "城市博物馆", "70", "70 元"),
        ("museum-low", "poi-museum", "城市博物馆", "50", "50 元"),
        ("restaurant", "poi-restaurant", "示例餐馆", "20.30", "20.30 元"),
        ("restaurant-dinner", "poi-restaurant", "示例餐馆", "25.75", "25.75 元"),
        ("free-restaurant", "poi-free-meal", "免费餐馆", "0", "0 元"),
        ("unknown-restaurant", "poi-unknown-meal", "未返回价格餐馆", None, None),
        ("tower", "poi-tower", "观景塔", "0.125", "0.125 元"),
        ("hotel", "poi-hotel", "外滩附近酒店", "399.50", "399.50 元"),
    )
    sources = tuple(
        FactSource(
            tool_call_id=f"call-{fact_id}",
            tool_name="maps_text_search",
            api_version="v4",
            queried_at=queried_at,
            effective_params={"keywords": name},
        )
        for fact_id, _, name, _, _ in fact_specs
    )
    places = tuple(
        PlaceFact(
            fact_id=f"fact-{fact_id}",
            poi_id=poi_id,
            name=name,
            cost=ReferenceCost(
                raw=raw,
                value=Decimal(value) if value is not None else None,
            ),
            source_refs=(f"call-{fact_id}",),
            field_sources={
                "poi_id": f"call-{fact_id}",
                **({"cost": f"call-{fact_id}"} if value is not None else {}),
            },
        )
        for fact_id, poi_id, name, value, raw in fact_specs
    )
    return FactStore(sources=sources, places=places)


def activity(activity_id: str, kind: str, poi_id: str, fact_id: str, slot_id: str) -> DraftActivity:
    return DraftActivity(
        activity_id=activity_id,
        candidate_ref=f"candidate-{fact_id}",
        slot_id=slot_id,
        kind=kind,
        poi_id=poi_id,
        place_fact_id=f"fact-{fact_id}",
        duration_s=3600,
        duration_source="model",
    )


def make_draft() -> PlanDraft:
    activities = (
        (
            activity("d1-bund-a", "sightseeing", "poi-bund", "bund-day1", "slot-1-morning"),
            activity("d1-bund-b", "sightseeing", "poi-bund", "bund-day1", "slot-1-afternoon"),
            activity("d1-lunch", "meal", "poi-restaurant", "restaurant", "slot-1-lunch"),
            activity("d1-dinner", "meal", "poi-restaurant", "restaurant-dinner", "slot-1-dinner"),
        ),
        (
            activity("d2-bund", "sightseeing", "poi-bund", "bund-day2", "slot-2-morning"),
            activity("d2-museum-high", "sightseeing", "poi-museum", "museum-high", "slot-2-afternoon"),
            activity("d2-museum-low", "sightseeing", "poi-museum", "museum-low", "slot-2-evening"),
            activity("d2-lunch", "meal", "poi-free-meal", "free-restaurant", "slot-2-lunch"),
            activity("d2-dinner", "meal", "poi-unknown-meal", "unknown-restaurant", "slot-2-dinner"),
        ),
        (
            activity("d3-tower", "sightseeing", "poi-tower", "tower", "slot-3-morning"),
            activity("d3-dinner", "meal", "poi-unknown-meal", "unknown-restaurant", "slot-3-dinner"),
        ),
    )
    draft_days = tuple(
        DraftDay(
            day_id=f"day-{index + 1}",
            day_date=day_date,
            activities=activities[index],
            lodging=LodgingRef(
                poi_id="poi-hotel",
                place_fact_id="fact-hotel",
                selection_source="recommended",
            ),
        )
        for index, day_date in enumerate(DAYS)
    )
    return PlanDraft(
        plan_id="cost-plan",
        request_id="cost-shanghai-3d",
        source_run_id="cost-run",
        revision=3,
        rule_version="cost-rules-1",
        days=draft_days,
    )


class ReferenceCostAggregationTest(unittest.TestCase):
    def setUp(self):
        self.planning_input = make_planning_input()
        self.draft = make_draft()
        self.facts = make_facts()

    def test_shanghai_three_day_fixture_uses_exact_dedup_and_meal_counts(self):
        before = tuple(
            value.model_dump_json()
            for value in (self.planning_input, self.draft, self.facts)
        )
        result = aggregate_reference_costs(self.planning_input, self.draft, self.facts)
        after = tuple(
            value.model_dump_json()
            for value in (self.planning_input, self.draft, self.facts)
        )

        self.assertEqual(before, after)
        self.assertEqual(result.status, "known_partial")
        self.assertEqual(
            result.known_sightseeing_meal_per_person_subtotal,
            Decimal("91.175"),
        )
        self.assertEqual(
            [day.known_sightseeing_meal_per_person_subtotal for day in result.days],
            [Decimal("68.55"), Decimal("22.50"), Decimal("0.125")],
        )
        self.assertEqual(
            [day.status for day in result.days],
            ["all_known", "known_partial", "known_partial"],
        )

        day1 = result.days[0]
        sightseeing = [entry for entry in day1.entries if entry.kind == "sightseeing"]
        meals = [entry for entry in day1.entries if entry.kind == "meal"]
        self.assertEqual(len(sightseeing), 1)
        self.assertEqual(sightseeing[0].activity_ids, ("d1-bund-a", "d1-bund-b"))
        self.assertEqual(sightseeing[0].amount, Decimal("22.50"))
        self.assertEqual(len(meals), 2)
        self.assertEqual([entry.amount for entry in meals], [Decimal("20.30"), Decimal("25.75")])

        day2_bund = next(entry for entry in result.days[1].entries if entry.poi_id == "poi-bund")
        self.assertEqual(day2_bund.amount, Decimal("22.50"))
        conflict = next(entry for entry in result.days[1].entries if entry.poi_id == "poi-museum")
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNone(conflict.amount)
        self.assertEqual(
            {evidence.cost.value for evidence in conflict.evidence},
            {Decimal("70"), Decimal("50")},
        )
        self.assertEqual(conflict.unknown_reason, "conflicting_values")

        free_entry = next(entry for entry in result.days[1].entries if entry.poi_id == "poi-free-meal")
        self.assertEqual(free_entry.status, "known")
        self.assertEqual(free_entry.amount, Decimal("0"))
        unknown = next(entry for entry in result.days[1].entries if entry.poi_id == "poi-unknown-meal")
        self.assertEqual(unknown.status, "unknown")
        self.assertIsNone(unknown.amount)

        evidence = sightseeing[0].evidence[0]
        self.assertEqual(evidence.cost.raw, "22.50 元")
        self.assertEqual(evidence.cost_source.tool_call_id, "call-bund-day1")
        self.assertEqual(
            evidence.cost_source.queried_at,
            datetime.fromisoformat("2026-10-10T08:05:00+08:00"),
        )
        self.assertEqual(
            [day.lodging.amount for day in result.days],
            [Decimal("399.50"), Decimal("399.50"), Decimal("399.50")],
        )

    def test_unknown_and_empty_activity_statuses_are_distinct(self):
        unknown_places = tuple(
            place.model_copy(update={"cost": ReferenceCost()})
            for place in self.facts.places
        )
        unknown_facts = self.facts.model_copy(update={"places": unknown_places})
        all_unknown = aggregate_reference_costs(self.planning_input, self.draft, unknown_facts)
        self.assertEqual(all_unknown.status, "all_unknown")
        self.assertEqual(all_unknown.known_sightseeing_meal_per_person_subtotal, Decimal("0"))

        empty_days = tuple(day.model_copy(update={"activities": ()}) for day in self.draft.days)
        empty_draft = self.draft.model_copy(update={"days": empty_days})
        no_activities = aggregate_reference_costs(self.planning_input, empty_draft, self.facts)
        self.assertEqual(no_activities.status, "no_activities")
        self.assertEqual(no_activities.known_sightseeing_meal_per_person_subtotal, Decimal("0"))
        self.assertTrue(all(day.status == "no_activities" for day in no_activities.days))
        self.assertTrue(all(day.lodging is not None for day in no_activities.days))

    def test_rejects_wrong_poi_fact_binding_and_dangling_cost_sources(self):
        bad_activity = self.draft.days[0].activities[0].model_copy(update={"poi_id": "poi-other"})
        bad_day = self.draft.days[0].model_copy(update={"activities": (bad_activity, *self.draft.days[0].activities[1:])})
        bad_draft = self.draft.model_copy(update={"days": (bad_day, *self.draft.days[1:])})
        with self.assertRaisesRegex(ReferenceCostAggregationError, "poi_id"):
            aggregate_reference_costs(self.planning_input, bad_draft, self.facts)

        bad_activity = self.draft.days[0].activities[0].model_copy(
            update={"place_fact_id": "missing-place-fact"}
        )
        bad_day = self.draft.days[0].model_copy(
            update={"activities": (bad_activity, *self.draft.days[0].activities[1:])}
        )
        bad_draft = self.draft.model_copy(update={"days": (bad_day, *self.draft.days[1:])})
        with self.assertRaisesRegex(ReferenceCostAggregationError, "不存在的地点事实"):
            aggregate_reference_costs(self.planning_input, bad_draft, self.facts)

        bad_place = self.facts.places[0].model_copy(update={"source_refs": ("missing-source",)})
        bad_places = (bad_place, *self.facts.places[1:])
        bad_facts = self.facts.model_copy(update={"places": bad_places})
        with self.assertRaisesRegex(ReferenceCostAggregationError, "source_refs"):
            aggregate_reference_costs(self.planning_input, self.draft, bad_facts)

        bad_place = self.facts.places[0].model_copy(
            update={"field_sources": {"cost": "missing-cost-source"}}
        )
        bad_places = (bad_place, *self.facts.places[1:])
        bad_facts = self.facts.model_copy(update={"places": bad_places})
        with self.assertRaisesRegex(ReferenceCostAggregationError, "field_sources.cost"):
            aggregate_reference_costs(self.planning_input, self.draft, bad_facts)


if __name__ == "__main__":
    unittest.main()
