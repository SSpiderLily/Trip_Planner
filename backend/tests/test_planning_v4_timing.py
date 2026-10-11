"""planning_v4 活动时间和返程计算的离线测试。"""
from datetime import date, datetime
import unittest

from app.models.planning_v4.facts import (
    FactSource,
    FactStore,
    MapCoordinate,
    PlaceFact,
    RouteFact,
    RouteOption,
)
from app.models.planning_v4.input import RequirementSummary, RuleSet, TripRequest
from app.models.planning_v4.plan import (
    DraftActivity,
    DraftDay,
    LegEndpoint,
    LodgingRef,
    PlanDraft,
    RouteLeg,
    RouteModeResult,
)
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_timing import calculate_plan_timing


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)


def make_input(
    arrival="2026-10-10T09:00:00+08:00",
    departure="2026-10-12T19:00:00+08:00",
    *,
    timezone_name="Asia/Shanghai",
    ready_at=None,
    departure_query="虹桥站",
    explicit_buffer=None,
):
    request = TripRequest(
        request_id="timing-request",
        city="上海",
        arrival_at=datetime.fromisoformat(arrival),
        departure_at=datetime.fromisoformat(departure),
        timezone=timezone_name,
        departure_place_query=departure_query,
        lodging_query="测试酒店",
    )
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=RequirementSummary(
            requirements_id="timing-requirements",
            request_id=request.request_id,
            core_goals=("城市漫步",),
        ),
        rules=RuleSet(
            rule_version="timing-rules-v1",
            windows=WINDOWS,
            meal_default_s=3600,
            sightseeing_default_s=10800,
            full_day_start_minute=8 * 60,
            return_buffer_default_s=3600,
            departure_day_minimum_usable_s=10800,
        ),
        explicit_return_buffer_s=explicit_buffer,
        day_ready_at=datetime.fromisoformat(ready_at) if ready_at else None,
    ))


def make_place(fact_id: str, poi_id: str) -> PlaceFact:
    return PlaceFact(
        fact_id=fact_id,
        poi_id=poi_id,
        name=poi_id,
        location=MapCoordinate(lng=121.0, lat=31.0),
        source_refs=("offline-fixture",),
    )


def make_facts(*, places=(), routes=()) -> FactStore:
    sources = {
        "offline-fixture": FactSource(
            tool_call_id="offline-fixture",
            tool_name="offline_fixture",
            api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
            effective_params={},
        )
    }
    for route in routes:
        if route is not None:
            sources[route.source.tool_call_id] = route.source
    return FactStore(sources=tuple(sources.values()), places=tuple(places), routes=tuple(route for route in routes if route is not None))


def make_lodging() -> LodgingRef:
    return LodgingRef(
        candidate_ref="candidate-hotel",
        poi_id="poi-hotel",
        place_fact_id="fact-hotel",
        selection_source="user",
    )


def make_days(planning_input, activities_by_date=None, *, lodging=True):
    activities_by_date = activities_by_date or {}
    result = []
    for constraint in planning_input.days:
        activities = []
        for spec in activities_by_date.get(constraint.day_date, ()):
            period, activity_id, poi_id, fact_id, kind, duration_s, duration_source = spec
            slot = next(item for item in constraint.slots if item.period == period)
            activities.append(DraftActivity(
                activity_id=activity_id,
                candidate_ref=None if kind == "free_time" else f"candidate-{activity_id}",
                slot_id=slot.slot_id,
                kind=kind,
                poi_id=None if kind == "free_time" else poi_id,
                place_fact_id=None if kind == "free_time" else fact_id,
                duration_s=duration_s,
                duration_source=duration_source,
            ))
        result.append(DraftDay(
            day_id=constraint.day_id,
            day_date=constraint.day_date,
            activities=tuple(activities),
            lodging=make_lodging() if lodging else None,
        ))
    return tuple(result)


def make_route_leg(
    leg_id,
    day_id,
    from_kind,
    from_ref,
    from_poi,
    to_kind,
    to_ref,
    to_poi,
    duration_s,
    *,
    source="backend_fastest_available",
    fact_origin=None,
    fact_destination=None,
    option_id=None,
):
    if source == "same_place":
        return RouteLeg(
            leg_id=leg_id,
            plan_revision=1,
            day_id=day_id,
            from_endpoint=LegEndpoint(kind=from_kind, reference_id=from_ref, poi_id=from_poi),
            to_endpoint=LegEndpoint(kind=to_kind, reference_id=to_ref, poi_id=to_poi),
            selection_source="same_place",
        ), None

    route_fact_id = f"route-fact-{leg_id}"
    option_id = option_id or f"option-{leg_id}"
    source_id = f"source-{leg_id}"
    route_fact = RouteFact(
        fact_id=route_fact_id,
        origin_poi_id=fact_origin or from_poi,
        destination_poi_id=fact_destination or to_poi,
        origin=MapCoordinate(lng=121.0, lat=31.0),
        destination=MapCoordinate(lng=121.0, lat=31.0),
        mode="walking",
        status="available",
        options=(RouteOption(
            option_id=option_id,
            duration_s=duration_s,
            geometry_status="missing",
        ),),
        source=FactSource(
            tool_call_id=source_id,
            tool_name="maps_direction_walking",
            api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
            effective_params={},
        ),
    )
    leg = RouteLeg(
        leg_id=leg_id,
        plan_revision=1,
        day_id=day_id,
        from_endpoint=LegEndpoint(kind=from_kind, reference_id=from_ref, poi_id=from_poi),
        to_endpoint=LegEndpoint(kind=to_kind, reference_id=to_ref, poi_id=to_poi),
        mode_results=(RouteModeResult(
            mode="walking",
            status="available",
            route_fact_id=route_fact_id,
            option_ids=(option_id,),
        ),),
        selected_mode="walking",
        selected_option_id=option_id,
        selection_source="backend_fastest_available",
    )
    return leg, route_fact


def make_pending_leg(leg_id, day_id, from_ref, from_poi, to_ref, to_poi):
    return RouteLeg(
        leg_id=leg_id,
        plan_revision=1,
        day_id=day_id,
        from_endpoint=LegEndpoint(kind="lodging" if from_ref.startswith("lodging:") else "activity", reference_id=from_ref, poi_id=from_poi),
        to_endpoint=LegEndpoint(kind="activity", reference_id=to_ref, poi_id=to_poi),
        selection_source=None,
    )


def make_draft(planning_input, days, legs):
    return PlanDraft(
        plan_id="timing-plan",
        request_id=planning_input.request.request_id,
        source_run_id="offline-run",
        revision=1,
        rule_version=planning_input.rules.rule_version,
        days=days,
        legs=tuple(legs),
    )


def full_day_fixture(*, model_duration=10800, include_first_leg=True):
    planning_input = make_input()
    day_date = date(2026, 10, 11)
    day_id = "day-2026-10-11"
    activity_specs = (
        ("breakfast", "a-breakfast", "poi-breakfast", "fact-breakfast", "meal", 3600, "default"),
        ("morning", "a-museum-am", "poi-museum", "fact-museum", "sightseeing", model_duration, "model"),
        ("lunch", "a-lunch", "poi-lunch", "fact-lunch", "meal", 3600, "default"),
        ("afternoon", "a-museum-pm", "poi-museum", "fact-museum", "sightseeing", 10800, "default"),
        ("dinner", "a-dinner", "poi-dinner", "fact-dinner", "meal", 3600, "default"),
        ("evening", "a-bund", "poi-bund", "fact-bund", "sightseeing", 10800, "default"),
    )
    activities = {day_date: activity_specs}
    days = make_days(planning_input, activities)
    day = next(item for item in days if item.day_date == day_date)
    transitions = (
        ("lodging", "lodging:" + day_id, "poi-hotel", "a-breakfast", "poi-breakfast", 600),
        ("activity", "a-breakfast", "poi-breakfast", "a-museum-am", "poi-museum", 1200),
        ("activity", "a-museum-am", "poi-museum", "a-lunch", "poi-lunch", 600),
        ("activity", "a-lunch", "poi-lunch", "a-museum-pm", "poi-museum", 1200),
        ("activity", "a-museum-pm", "poi-museum", "a-dinner", "poi-dinner", 600),
        ("activity", "a-dinner", "poi-dinner", "a-bund", "poi-bund", 1200),
    )
    legs, route_facts = [], []
    for index, (from_kind, from_ref, from_poi, to_ref, to_poi, seconds) in enumerate(transitions):
        if index == 0 and not include_first_leg:
            continue
        leg, fact = make_route_leg(
            f"leg-full-{index}", day_id, from_kind, from_ref, from_poi,
            "activity", to_ref, to_poi, seconds,
        )
        legs.append(leg)
        route_facts.append(fact)
    places = [
        make_place("fact-hotel", "poi-hotel"),
        make_place("fact-breakfast", "poi-breakfast"),
        make_place("fact-museum", "poi-museum"),
        make_place("fact-lunch", "poi-lunch"),
        make_place("fact-dinner", "poi-dinner"),
        make_place("fact-bund", "poi-bund"),
        make_place("fact-terminal", "poi-terminal"),
    ]
    return planning_input, make_draft(planning_input, days, legs), make_facts(places=places, routes=route_facts)


class PlanningV4TimingTests(unittest.TestCase):
    def test_shanghai_full_day_fixed_timing_sample(self):
        planning_input, draft, facts = full_day_fixture()
        result = calculate_plan_timing(planning_input, draft, facts)
        day = next(item for item in result.days if item.day_id == "day-2026-10-11")
        by_id = {item.activity_id: item for item in day.activities}

        self.assertEqual(day.day_start_at.isoformat(), "2026-10-11T08:00:00+08:00")
        self.assertEqual(by_id["a-breakfast"].start_at.isoformat(), "2026-10-11T08:10:00+08:00")
        self.assertEqual(by_id["a-breakfast"].end_at.isoformat(), "2026-10-11T09:10:00+08:00")
        self.assertEqual(by_id["a-museum-am"].start_at.isoformat(), "2026-10-11T09:30:00+08:00")
        self.assertEqual(by_id["a-museum-am"].end_at.isoformat(), "2026-10-11T12:30:00+08:00")
        self.assertEqual(by_id["a-lunch"].start_at.isoformat(), "2026-10-11T12:40:00+08:00")
        self.assertEqual(by_id["a-museum-pm"].start_at.isoformat(), "2026-10-11T14:00:00+08:00")
        self.assertEqual(by_id["a-dinner"].start_at.isoformat(), "2026-10-11T17:10:00+08:00")
        self.assertEqual(by_id["a-bund"].arrive_at.isoformat(), "2026-10-11T18:30:00+08:00")
        self.assertEqual(by_id["a-bund"].start_at.isoformat(), "2026-10-11T19:00:00+08:00")
        self.assertEqual(by_id["a-bund"].wait_s, 1800)
        self.assertEqual(day.summary.known_stay_s, 12 * 3600)
        self.assertEqual(day.summary.known_travel_s, 90 * 60)
        self.assertEqual(day.wait_s, 30 * 60)
        self.assertEqual(day.elapsed_s, 14 * 3600)

    def test_model_duration_wins_and_zero_is_preserved(self):
        planning_input, draft, facts = full_day_fixture(model_duration=7200)
        result = calculate_plan_timing(planning_input, draft, facts)
        day = next(item for item in result.days if item.day_id == "day-2026-10-11")
        museum = next(item for item in day.activities if item.activity_id == "a-museum-am")
        self.assertEqual(museum.duration_s, 7200)
        self.assertEqual(museum.duration_source, "model")
        self.assertEqual(museum.end_at.isoformat(), "2026-10-11T11:30:00+08:00")

        zero_activity = DraftActivity.model_construct(
            activity_id="zero-duration",
            candidate_ref="candidate-zero",
            slot_id=next(slot.slot_id for slot in planning_input.days[1].slots if slot.period == "breakfast"),
            kind="sightseeing",
            poi_id="poi-zero",
            place_fact_id="fact-zero",
            duration_s=0,
            duration_source="model",
        )
        day_date = date(2026, 10, 11)
        days = make_days(planning_input)
        target_index = next(i for i, item in enumerate(days) if item.day_date == day_date)
        days = list(days)
        days[target_index] = days[target_index].model_copy(update={"activities": (zero_activity,)})
        leg, route_fact = make_route_leg(
            "zero-leg", days[target_index].day_id, "lodging", "lodging:" + days[target_index].day_id,
            "poi-hotel", "activity", "zero-duration", "poi-zero", 600,
        )
        facts = make_facts(
            places=facts.places + (make_place("fact-zero", "poi-zero"),),
            routes=(route_fact,),
        )
        zero_result = calculate_plan_timing(planning_input, make_draft(planning_input, tuple(days), (leg,)), facts)
        zero_timing = next(item for item in zero_result.days if item.day_id == days[target_index].day_id).activities[0]
        self.assertEqual(zero_timing.duration_s, 0)
        self.assertEqual(zero_timing.end_at, zero_timing.start_at)

    def test_unknown_leg_propagates_without_becoming_zero(self):
        planning_input, draft, facts = full_day_fixture(include_first_leg=False)
        result = calculate_plan_timing(planning_input, draft, facts)
        day = next(item for item in result.days if item.day_id == "day-2026-10-11")

        self.assertIsNone(day.activities[0].arrive_at)
        self.assertTrue(all(item.start_at is None and item.end_at is None for item in day.activities))
        self.assertEqual(day.activities[0].unknown_reason, "missing_route_leg")
        self.assertEqual(day.summary.known_travel_s, 80 * 60)
        self.assertEqual(day.scheduling_decision, "conditional")
        self.assertEqual(day.unknown_transitions[0].reason, "missing_route_leg")

    def test_first_day_needs_known_ready_time_and_uses_local_timezone(self):
        planning_input = make_input(
            arrival="2026-10-10T16:00:00+00:00",
            departure="2026-10-13T02:00:00+00:00",
            timezone_name="America/Los_Angeles",
            ready_at="2026-10-10T10:30:00-07:00",
        )
        days = make_days(planning_input)
        draft = make_draft(planning_input, days, ())
        facts = make_facts(places=(make_place("fact-hotel", "poi-hotel"),))
        result = calculate_plan_timing(planning_input, draft, facts)

        self.assertEqual(result.days[0].day_start_at.isoformat(), "2026-10-10T10:30:00-07:00")
        self.assertEqual(result.days[1].day_start_at.isoformat(), "2026-10-11T08:00:00-07:00")
        self.assertEqual(result.days[2].day_start_at.isoformat(), "2026-10-12T08:00:00-07:00")

        unknown_ready_input = make_input(
            arrival="2026-10-10T16:00:00+00:00",
            departure="2026-10-13T02:00:00+00:00",
            timezone_name="America/Los_Angeles",
        )
        unknown_result = calculate_plan_timing(
            unknown_ready_input,
            make_draft(unknown_ready_input, make_days(unknown_ready_input), ()),
            facts,
        )
        self.assertIsNone(unknown_result.days[0].day_start_at)

    def test_departure_day_exact_three_hours_is_not_skipped(self):
        planning_input = make_input(
            arrival="2026-10-11T10:00:00+08:00",
            departure="2026-10-12T12:00:00+08:00",
            departure_query=None,
        )
        day_date = date(2026, 10, 12)
        spec = ("breakfast", "a-final", "poi-final", "fact-final", "meal", 3600, "default")
        days = make_days(planning_input, {day_date: (spec,)})
        leg, route_fact = make_route_leg(
            "exact-three-hours-leg", "day-2026-10-12", "lodging", "lodging:day-2026-10-12",
            "poi-hotel", "activity", "a-final", "poi-final", 60,
        )
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-final", "poi-final")),
            routes=(route_fact,),
        )
        result = calculate_plan_timing(planning_input, make_draft(planning_input, days, (leg,)), facts)
        departure_day = result.days[-1]

        self.assertEqual(departure_day.return_timing.deadline_at.isoformat(), "2026-10-12T11:00:00+08:00")
        self.assertEqual(departure_day.scheduling_decision, "conditional")
        self.assertNotEqual(departure_day.scheduling_decision, "not_scheduled_insufficient_time")

    def test_departure_day_just_below_three_hours_is_not_scheduled(self):
        planning_input = make_input(
            arrival="2026-10-11T10:00:00+08:00",
            departure="2026-10-12T11:59:00+08:00",
            departure_query=None,
        )
        day_date = date(2026, 10, 12)
        spec = ("breakfast", "a-final", "poi-final", "fact-final", "meal", 3600, "default")
        days = make_days(planning_input, {day_date: (spec,)})
        facts = make_facts(places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-final", "poi-final")))
        draft = make_draft(planning_input, days, ())
        result = calculate_plan_timing(planning_input, draft, facts)
        departure_day = result.days[-1]

        self.assertEqual(departure_day.scheduling_decision, "not_scheduled_insufficient_time")
        self.assertEqual(departure_day.activities[0].unknown_reason, "departure_day_under_minimum_usable_time")
        self.assertEqual(len(draft.days[-1].activities), 1)
        self.assertIsNone(departure_day.activities[0].start_at)

    def test_known_return_travel_is_separate_from_buffer_and_can_trigger_short_day(self):
        planning_input = make_input(
            arrival="2026-10-11T10:00:00+08:00",
            departure="2026-10-12T12:00:00+08:00",
        )
        day_date = date(2026, 10, 12)
        spec = ("breakfast", "a-final", "poi-final", "fact-final", "meal", 3600, "default")
        days = make_days(planning_input, {day_date: (spec,)})
        out_leg, out_fact = make_route_leg(
            "short-out", "day-2026-10-12", "lodging", "lodging:day-2026-10-12",
            "poi-hotel", "activity", "a-final", "poi-final", 60,
        )
        return_leg, return_fact = make_route_leg(
            "short-return", "day-2026-10-12", "activity", "a-final", "poi-final",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-final", "poi-final"), make_place("fact-terminal", "poi-terminal")),
            routes=(out_fact, return_fact),
        )
        result = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, (out_leg, return_leg)),
            facts,
            verified_departure_fact_id="fact-terminal",
        )
        day = result.days[-1]

        self.assertEqual(day.return_timing.buffer_s, 3600)
        self.assertEqual(day.return_timing.travel_s, 1200)
        self.assertEqual(day.return_timing.usable_s, 9600)
        self.assertEqual(day.return_timing.latest_activity_end_at.isoformat(), "2026-10-12T10:40:00+08:00")
        self.assertEqual(day.scheduling_decision, "not_scheduled_insufficient_time")

    def test_same_day_arrival_and_departure_use_known_ready_time_for_three_hour_bound(self):
        planning_input = make_input(
            arrival="2026-10-12T08:00:00+08:00",
            departure="2026-10-12T19:00:00+08:00",
            ready_at="2026-10-12T15:00:00+08:00",
        )
        day_date = date(2026, 10, 12)
        spec = ("afternoon", "a-final", "poi-final", "fact-final", "meal", 0, "model")
        days = make_days(planning_input, {day_date: (spec,)})
        returning, return_fact = make_route_leg(
            "same-day-return", "day-2026-10-12", "activity", "a-final", "poi-final",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-final", "poi-final"), make_place("fact-terminal", "poi-terminal")),
            routes=(return_fact,),
        )
        draft = make_draft(planning_input, days, (returning,))
        known_return = calculate_plan_timing(
            planning_input, draft, facts, verified_departure_fact_id="fact-terminal"
        ).days[0]

        self.assertEqual(known_return.day_start_at.isoformat(), "2026-10-12T15:00:00+08:00")
        self.assertEqual(known_return.return_timing.usable_s, 2 * 3600 + 40 * 60)
        self.assertEqual(known_return.return_timing.optimistic_usable_s, 2 * 3600 + 40 * 60)
        self.assertEqual(known_return.scheduling_decision, "not_scheduled_insufficient_time")
        self.assertEqual(len(draft.days[0].activities), 1)

        unknown_return = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, ()),
            make_facts(places=(make_place("fact-final", "poi-final"),)),
        ).days[0]
        self.assertEqual(unknown_return.return_timing.optimistic_usable_s, 3 * 3600)
        self.assertEqual(unknown_return.scheduling_decision, "conditional")

    def test_return_deadline_is_available_when_forward_chain_is_unknown(self):
        planning_input = make_input()
        day_date = date(2026, 10, 12)
        specs = (
            ("breakfast", "a-first", "poi-first", "fact-first", "meal", 3600, "default"),
            ("lunch", "a-last", "poi-last", "fact-last", "meal", 3600, "default"),
        )
        days = make_days(planning_input, {day_date: specs})
        unknown_out = make_pending_leg(
            "unknown-out", "day-2026-10-12", "lodging:day-2026-10-12", "poi-hotel", "a-first", "poi-first"
        )
        middle, middle_fact = make_route_leg(
            "known-middle", "day-2026-10-12", "activity", "a-first", "poi-first",
            "activity", "a-last", "poi-last", 600,
        )
        returning, return_fact = make_route_leg(
            "known-return", "day-2026-10-12", "activity", "a-last", "poi-last",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-first", "poi-first"),
                    make_place("fact-last", "poi-last"), make_place("fact-terminal", "poi-terminal")),
            routes=(middle_fact, return_fact),
        )
        result = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, (unknown_out, middle, returning)),
            facts,
            verified_departure_fact_id="fact-terminal",
        )
        day = result.days[-1]

        self.assertEqual(day.return_timing.deadline_at.isoformat(), "2026-10-12T18:00:00+08:00")
        self.assertEqual(day.return_timing.travel_s, 1200)
        self.assertEqual(day.return_timing.latest_activity_end_at.isoformat(), "2026-10-12T17:40:00+08:00")
        self.assertEqual(day.return_timing.status, "unverified")
        self.assertIn("terminal_activity_end_unknown", day.return_timing.unknown_reasons)
        self.assertEqual(day.summary.unknown_leg_ids, ("unknown-out",))

    def test_unverified_return_endpoint_does_not_prove_feasibility(self):
        # 请求文本和 FactStore 中存在某个 POI，都不能替代 F04d 的显式事实绑定。
        planning_input = make_input()
        day_date = date(2026, 10, 12)
        spec = ("breakfast", "a-final", "poi-final", "fact-final", "meal", 3600, "default")
        days = make_days(planning_input, {day_date: (spec,)})
        out_leg, out_fact = make_route_leg(
            "endpoint-out", "day-2026-10-12", "lodging", "lodging:day-2026-10-12",
            "poi-hotel", "activity", "a-final", "poi-final", 60,
        )
        return_leg, return_fact = make_route_leg(
            "endpoint-return", "day-2026-10-12", "activity", "a-final", "poi-final",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-final", "poi-final"), make_place("fact-terminal", "poi-terminal")),
            routes=(out_fact, return_fact),
        )
        day = calculate_plan_timing(planning_input, make_draft(planning_input, days, (out_leg, return_leg)), facts).days[-1]

        self.assertEqual(day.return_timing.status, "unverified")
        self.assertIsNotNone(day.return_timing.deadline_at)
        self.assertIsNone(day.return_timing.departure_poi_id)
        self.assertIn("departure_fact_not_bound", day.return_timing.unknown_reasons)
        self.assertIsNone(day.return_timing.travel_s)
        self.assertEqual(day.scheduling_decision, "conditional")

    def test_known_late_return_is_reported_as_conflict(self):
        planning_input = make_input()
        day_date = date(2026, 10, 12)
        spec = ("evening", "a-final", "poi-final", "fact-final", "sightseeing", 7200, "model")
        days = make_days(planning_input, {day_date: (spec,)})
        outbound, outbound_fact = make_route_leg(
            "late-out", "day-2026-10-12", "lodging", "lodging:day-2026-10-12",
            "poi-hotel", "activity", "a-final", "poi-final", 39000,
        )
        returning, return_fact = make_route_leg(
            "late-return", "day-2026-10-12", "activity", "a-final", "poi-final",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-final", "poi-final"), make_place("fact-terminal", "poi-terminal")),
            routes=(outbound_fact, return_fact),
        )
        day = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, (outbound, returning)),
            facts,
            verified_departure_fact_id="fact-terminal",
        ).days[-1]

        self.assertEqual(day.activities[0].start_at.isoformat(), "2026-10-12T19:00:00+08:00")
        self.assertEqual(day.return_timing.estimated_arrival_at.isoformat(), "2026-10-12T21:20:00+08:00")
        self.assertEqual(day.return_timing.status, "conflict")
        self.assertEqual(day.return_timing.overrun_s, 3 * 3600 + 20 * 60)
        self.assertEqual(day.summary.feasibility, "conflict")

    def test_same_place_zero_requires_verified_identity(self):
        planning_input = make_input()
        day_date = date(2026, 10, 11)
        specs = (
            ("breakfast", "a-one", "poi-shared", "fact-shared", "sightseeing", 0, "model"),
            ("morning", "a-two", "poi-shared", "fact-shared", "sightseeing", 0, "model"),
        )
        days = make_days(planning_input, {day_date: specs})
        day = next(item for item in days if item.day_date == day_date)
        outbound, outbound_fact = make_route_leg(
            "shared-out", day.day_id, "lodging", f"lodging:{day.day_id}", "poi-hotel",
            "activity", "a-one", "poi-shared", 600,
        )
        same, _ = make_route_leg(
            "shared-same", day.day_id, "activity", "a-one", "poi-shared",
            "activity", "a-two", "poi-shared", 0, source="same_place",
        )
        facts = make_facts(places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-shared", "poi-shared")), routes=(outbound_fact,))
        timing = calculate_plan_timing(planning_input, make_draft(planning_input, days, (outbound, same)), facts).days[1]
        self.assertEqual(timing.activities[1].travel_s, 0)
        self.assertEqual(timing.activities[1].status, "estimated")

        unverified_fact = make_place("fact-shared", "poi-shared").model_copy(update={"location": None})
        bad_facts = make_facts(places=(make_place("fact-hotel", "poi-hotel"), unverified_fact), routes=(outbound_fact,))
        bad_timing = calculate_plan_timing(planning_input, make_draft(planning_input, days, (outbound, same)), bad_facts).days[1]
        self.assertIsNone(bad_timing.activities[1].travel_s)
        self.assertEqual(bad_timing.activities[1].unknown_reason, "same_place_identity_unverified")

    def test_route_fact_must_match_selected_direction_and_endpoints(self):
        planning_input = make_input()
        day_date = date(2026, 10, 11)
        specs = (("breakfast", "a-one", "poi-one", "fact-one", "meal", 3600, "default"),)
        days = make_days(planning_input, {day_date: specs})
        leg, route_fact = make_route_leg(
            "wrong-direction", "day-2026-10-11", "lodging", "lodging:day-2026-10-11", "poi-hotel",
            "activity", "a-one", "poi-one", 600, fact_origin="poi-one", fact_destination="poi-hotel",
        )
        facts = make_facts(places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-one", "poi-one")), routes=(route_fact,))

        with self.assertRaisesRegex(ValueError, "方向、端点或方式不一致"):
            calculate_plan_timing(planning_input, make_draft(planning_input, days, (leg,)), facts)

    def test_route_coordinates_must_match_bound_place_facts(self):
        planning_input = make_input()
        day_date = date(2026, 10, 11)
        specs = (("breakfast", "a-one", "poi-one", "fact-one", "meal", 3600, "default"),)
        days = make_days(planning_input, {day_date: specs})
        leg, route_fact = make_route_leg(
            "wrong-coordinates", "day-2026-10-11", "lodging", "lodging:day-2026-10-11", "poi-hotel",
            "activity", "a-one", "poi-one", 600,
        )
        route_fact = route_fact.model_copy(update={"destination": MapCoordinate(lng=121.5, lat=31.5)})
        facts = make_facts(
            places=(make_place("fact-hotel", "poi-hotel"), make_place("fact-one", "poi-one")),
            routes=(route_fact,),
        )
        with self.assertRaisesRegex(ValueError, "路线坐标与草稿端点地点事实不一致"):
            calculate_plan_timing(planning_input, make_draft(planning_input, days, (leg,)), facts)

    def test_fact_store_source_references_must_be_closed(self):
        planning_input, draft, facts = full_day_fixture()
        unclosed = FactStore(places=facts.places, routes=facts.routes)
        with self.assertRaisesRegex(ValueError, "来源没有在 FactStore 中闭合|不存在的来源"):
            calculate_plan_timing(planning_input, draft, unclosed)

    def test_free_time_advances_time_without_becoming_route_endpoint(self):
        planning_input = make_input()
        day_date = date(2026, 10, 12)
        specs = (
            ("breakfast", "a-one", "poi-one", "fact-one", "sightseeing", 3600, "model"),
            ("morning", "free-mid", None, None, "free_time", 1800, "user"),
            ("lunch", "a-two", "poi-two", "fact-two", "sightseeing", 0, "model"),
            ("dinner", "free-end", None, None, "free_time", 1800, "user"),
        )
        days = make_days(planning_input, {day_date: specs})
        day = next(item for item in days if item.day_date == day_date)
        outbound, outbound_fact = make_route_leg(
            "free-out", day.day_id, "lodging", f"lodging:{day.day_id}", "poi-hotel",
            "activity", "a-one", "poi-one", 600,
        )
        after_free, after_free_fact = make_route_leg(
            "free-skip", day.day_id, "activity", "a-one", "poi-one",
            "activity", "a-two", "poi-two", 600,
        )
        returning, return_fact = make_route_leg(
            "free-return", day.day_id, "activity", "a-two", "poi-two",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(
                make_place("fact-hotel", "poi-hotel"),
                make_place("fact-one", "poi-one"),
                make_place("fact-two", "poi-two"),
                make_place("fact-terminal", "poi-terminal"),
            ),
            routes=(outbound_fact, after_free_fact, return_fact),
        )
        draft = make_draft(planning_input, days, (outbound, after_free, returning))
        result = calculate_plan_timing(
            planning_input, draft, facts, verified_departure_fact_id="fact-terminal"
        ).days[-1]
        by_id = {item.activity_id: item for item in result.activities}

        self.assertEqual(by_id["free-mid"].duration_s, 1800)
        self.assertEqual(by_id["free-mid"].arrive_at.isoformat(), "2026-10-12T09:10:00+08:00")
        self.assertEqual(by_id["free-mid"].end_at.isoformat(), "2026-10-12T09:40:00+08:00")
        self.assertIsNone(by_id["free-mid"].travel_s)
        self.assertEqual(by_id["a-two"].incoming_leg_id, "free-skip")
        self.assertEqual(by_id["a-two"].arrive_at.isoformat(), "2026-10-12T09:50:00+08:00")
        self.assertEqual(by_id["free-end"].end_at.isoformat(), "2026-10-12T17:30:00+08:00")
        self.assertEqual(result.return_timing.estimated_arrival_at.isoformat(), "2026-10-12T17:50:00+08:00")
        self.assertIn("未计入自行离开所在区域的移动", result.return_timing.notes[0])
        self.assertIn("free_time_movement_not_included", result.return_timing.unknown_reasons)
        self.assertEqual(result.return_timing.status, "unverified")
        self.assertEqual(result.summary.known_travel_s, 40 * 60)

        invalid_free_endpoint, _ = make_route_leg(
            "invalid-free-endpoint", day.day_id, "activity", "free-mid", "poi-forged",
            "activity", "a-two", "poi-two", 30,
        )
        with self.assertRaisesRegex(ValueError, "free_time 活动作为路线端点"):
            calculate_plan_timing(
                planning_input,
                make_draft(planning_input, days, (outbound, after_free, returning, invalid_free_endpoint)),
                facts,
                verified_departure_fact_id="fact-terminal",
            )

    def test_unlocated_lodging_does_not_turn_first_route_into_zero(self):
        planning_input = make_input()
        day_date = date(2026, 10, 12)
        specs = (("breakfast", "a-first", "poi-first", "fact-first", "meal", 3600, "default"),)
        days = make_days(planning_input, {day_date: specs}, lodging=False)
        leg, route_fact = make_route_leg(
            "hotel-unknown", "day-2026-10-12", "lodging", "lodging:day-2026-10-12", "poi-hotel",
            "activity", "a-first", "poi-first", 600,
        )
        facts = make_facts(places=(make_place("fact-first", "poi-first"),), routes=(route_fact,))
        day = calculate_plan_timing(planning_input, make_draft(planning_input, days, (leg,)), facts).days[-1]

        self.assertEqual(day.day_start_at.isoformat(), "2026-10-12T08:00:00+08:00")
        self.assertIsNone(day.activities[0].travel_s)
        self.assertEqual(day.activities[0].unknown_reason, "lodging_unverified")
        self.assertEqual(day.scheduling_decision, "conditional")

    def test_known_return_conflict_survives_unknown_forward_time(self):
        planning_input = make_input(departure="2026-10-12T18:00:00+08:00")
        day_date = date(2026, 10, 12)
        specs = (("evening", "a-final", "poi-final", "fact-final", "sightseeing", 3600, "model"),)
        days = make_days(planning_input, {day_date: specs})
        returning, return_fact = make_route_leg(
            "known-return-after-unknown", "day-2026-10-12", "activity", "a-final", "poi-final",
            "departure", "station", "poi-terminal", 1200,
        )
        facts = make_facts(
            places=(make_place("fact-final", "poi-final"), make_place("fact-terminal", "poi-terminal")),
            routes=(return_fact,),
        )
        day = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, (returning,)),
            facts,
            verified_departure_fact_id="fact-terminal",
        ).days[-1]

        self.assertIsNone(day.activities[0].end_at)
        self.assertEqual(day.return_timing.deadline_at.isoformat(), "2026-10-12T17:00:00+08:00")
        self.assertEqual(day.return_timing.status, "conflict")
        self.assertIsNone(day.return_timing.estimated_arrival_at)
        self.assertEqual(day.return_timing.earliest_possible_arrival_at.isoformat(), "2026-10-12T20:20:00+08:00")
        self.assertIn("terminal_activity_end_unknown", day.return_timing.unknown_reasons)
        self.assertIn("minimum_return_arrival_exceeds_deadline", day.return_timing.unknown_reasons)

        unknown_return_day = calculate_plan_timing(
            planning_input,
            make_draft(planning_input, days, ()),
            make_facts(),
        ).days[-1]
        self.assertEqual(unknown_return_day.return_timing.status, "conflict")
        self.assertIn("minimum_activity_end_exceeds_return_deadline", unknown_return_day.return_timing.unknown_reasons)
        self.assertIn("return_route_missing", unknown_return_day.return_timing.unknown_reasons)

    def test_negative_duration_is_rejected(self):
        planning_input = make_input()
        day_date = date(2026, 10, 11)
        slot_id = next(slot.slot_id for slot in planning_input.days[1].slots if slot.period == "breakfast")
        invalid = DraftActivity.model_construct(
            activity_id="negative",
            candidate_ref="candidate-negative",
            slot_id=slot_id,
            kind="sightseeing",
            poi_id="poi-negative",
            place_fact_id="fact-negative",
            duration_s=-1,
            duration_source="model",
        )
        days = make_days(planning_input)
        index = next(i for i, day in enumerate(days) if day.day_date == day_date)
        days = list(days)
        days[index] = days[index].model_copy(update={"activities": (invalid,)})
        draft = make_draft(planning_input, tuple(days), ())

        with self.assertRaisesRegex(ValueError, "不能为负数"):
            calculate_plan_timing(planning_input, draft, FactStore())

    def test_missing_duration_is_not_replaced_by_a_default(self):
        planning_input = make_input()
        day_date = date(2026, 10, 11)
        days = make_days(planning_input)
        index = next(i for i, item in enumerate(days) if item.day_date == day_date)
        slot_id = next(slot.slot_id for slot in planning_input.days[index].slots if slot.period == "breakfast")
        activity = DraftActivity.model_construct(
            activity_id="missing-duration",
            candidate_ref="candidate-missing-duration",
            slot_id=slot_id,
            kind="sightseeing",
            poi_id="poi-missing-duration",
            place_fact_id="fact-missing-duration",
            duration_s=None,
            duration_source="default",
        )
        days = list(days)
        days[index] = days[index].model_copy(update={"activities": (activity,)})
        with self.assertRaisesRegex(ValueError, "必须提供停留时长"):
            calculate_plan_timing(planning_input, make_draft(planning_input, tuple(days), ()), FactStore())


if __name__ == "__main__":
    unittest.main()
