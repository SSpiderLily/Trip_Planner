from datetime import date, datetime
import unittest

from pydantic import ValidationError

from app.models.planning_v4 import PlanningInput, RequirementSummary, RuleSet, TripRequest
from app.services.planning_input_builder import (
    PlanningInputBuildRequest,
    build_planning_input,
)


TZ = "Asia/Shanghai"
WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
PERIODS = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")


def make_rules(**overrides):
    values = {
        "rule_version": "f02-test-v1",
        "windows": WINDOWS,
        "meal_default_s": 3600,
        "sightseeing_default_s": 10800,
        "full_day_start_minute": 8 * 60,
        "return_buffer_default_s": 3600,
        "departure_day_minimum_usable_s": 10800,
    }
    values.update(overrides)
    return RuleSet(**values)


def make_request(arrival, departure, *, timezone_name=TZ):
    return TripRequest(
        request_id="f02-request",
        city="上海",
        arrival_at=datetime.fromisoformat(arrival),
        departure_at=datetime.fromisoformat(departure),
        timezone=timezone_name,
        arrival_place_query=None,
        departure_place_query=None,
    )


def make_summary(request):
    return RequirementSummary(
        requirements_id="f02-summary",
        request_id=request.request_id,
        core_goals=("城市漫步", "美食"),
        unknowns=("到达后实际就绪时刻", "返程地点与交通"),
    )


def make_input(
    arrival="2026-10-10T09:00:00+08:00",
    departure="2026-10-12T19:00:00+08:00",
    *,
    rules=None,
    explicit_return_buffer_s=None,
    day_ready_at=None,
):
    request = make_request(arrival, departure)
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=make_summary(request),
        rules=rules or make_rules(),
        explicit_return_buffer_s=explicit_return_buffer_s,
        day_ready_at=datetime.fromisoformat(day_ready_at) if day_ready_at else None,
    ))


def slot_map(day):
    return {slot.period: slot for slot in day.slots}


class PlanningInputBuilderTests(unittest.TestCase):
    def test_shanghai_three_day_reference(self):
        result = make_input()

        self.assertEqual(result.request.request_id, result.requirements.request_id)
        self.assertEqual([day.day_date for day in result.days], [
            date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12),
        ])
        self.assertEqual([len(day.slots) for day in result.days], [6, 6, 6])
        self.assertEqual([slot.period for slot in result.days[0].slots], list(PERIODS))
        self.assertIsNone(result.days[0].available_start)
        self.assertEqual(result.days[1].available_start.isoformat(), "2026-10-11T08:00:00+08:00")
        self.assertEqual(result.days[2].available_end, None)
        self.assertEqual(result.boundary_facts.effective_return_buffer_s, 3600)
        self.assertEqual(result.boundary_facts.return_buffer_source, "default")
        self.assertEqual(
            result.boundary_facts.arrival_start_optimistic_at.isoformat(),
            "2026-10-10T09:00:00+08:00",
        )
        self.assertEqual(
            result.boundary_facts.departure_activity_end_upper_bound_at.isoformat(),
            "2026-10-12T18:00:00+08:00",
        )

        day1, day2, day3 = (slot_map(day) for day in result.days)
        self.assertEqual(
            {period: slot.eligibility for period, slot in day1.items()},
            {
                "breakfast": "skipped",
                "morning": "skipped",
                "lunch": "conditional",
                "afternoon": "required",
                "dinner": "required",
                "evening": "required",
            },
        )
        self.assertEqual({slot.eligibility for slot in day2.values()}, {"required"})
        self.assertEqual(
            {period: slot.eligibility for period, slot in day3.items()},
            {
                "breakfast": "required",
                "morning": "required",
                "lunch": "required",
                "afternoon": "required",
                "dinner": "conditional",
                "evening": "skipped",
            },
        )
        self.assertIsNotNone(day1["lunch"].condition_reason)
        self.assertIn("返程", day3["dinner"].condition_reason)

    def test_arrival_boundaries_are_half_open(self):
        cases = (
            ("00:00:00", ("skipped", "required", "required")),
            ("06:00:00", ("skipped", "required", "required")),
            ("12:00:00", ("skipped", "skipped", "required")),
            ("18:00:00", ("skipped", "skipped", "skipped")),
        )
        for clock, expected in cases:
            with self.subTest(clock=clock):
                result = make_input(
                    f"2026-10-10T{clock}+08:00",
                    "2026-10-11T19:00:00+08:00",
                )
                first_day = slot_map(result.days[0])
                self.assertEqual(
                    tuple(first_day[period].eligibility for period in ("morning", "afternoon", "evening")),
                    expected,
                )

    def test_arrival_uses_destination_timezone_for_dates_and_boundaries(self):
        request = make_request("2026-10-09T22:00:00+00:00", "2026-10-12T11:00:00+00:00")
        result = build_planning_input(PlanningInputBuildRequest(
            request=request,
            requirements=make_summary(request),
            rules=make_rules(),
        ))

        self.assertEqual([day.day_date for day in result.days], [
            date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12),
        ])
        self.assertIsNone(result.days[0].available_start)
        self.assertEqual(
            result.boundary_facts.arrival_start_optimistic_at.isoformat(),
            "2026-10-10T06:00:00+08:00",
        )
        self.assertEqual(slot_map(result.days[0])["morning"].eligibility, "skipped")

    def test_same_day_arrival_and_departure_combine_and_exact_three_hours_survives(self):
        exactly_three_hours = make_input(
            "2026-10-10T08:00:00+08:00",
            "2026-10-10T12:00:00+08:00",
            explicit_return_buffer_s=3600,
        )
        breakfast = slot_map(exactly_three_hours.days[0])["breakfast"]
        self.assertEqual(breakfast.eligibility, "conditional")
        self.assertIn("到达", breakfast.condition_reason)

        below_three_hours = make_input(
            "2026-10-10T08:00:00+08:00",
            "2026-10-10T11:59:00+08:00",
            explicit_return_buffer_s=3600,
        )
        self.assertEqual({slot.eligibility for slot in below_three_hours.days[0].slots}, {"skipped"})

    def test_explicit_buffer_overrides_default_without_parsing_remarks(self):
        rules = make_rules(return_buffer_default_s=3600)
        defaulted = make_input(
            "2026-10-10T08:00:00+08:00",
            "2026-10-10T12:00:00+08:00",
            rules=rules,
        )
        explicit_two_hours = make_input(
            "2026-10-10T08:00:00+08:00",
            "2026-10-10T12:00:00+08:00",
            rules=rules,
            explicit_return_buffer_s=7200,
        )
        self.assertEqual(slot_map(defaulted.days[0])["breakfast"].eligibility, "conditional")
        self.assertEqual({slot.eligibility for slot in explicit_two_hours.days[0].slots}, {"skipped"})
        self.assertEqual(explicit_two_hours.boundary_facts.effective_return_buffer_s, 7200)
        self.assertEqual(explicit_two_hours.boundary_facts.return_buffer_source, "explicit_user")

        restored = PlanningInput.model_validate_json(explicit_two_hours.model_dump_json())
        self.assertEqual(restored.boundary_facts, explicit_two_hours.boundary_facts)

    def test_short_meal_window_skips_but_exact_one_hour_is_conditional(self):
        one_hour = make_input(
            "2026-10-10T13:00:00+08:00",
            "2026-10-11T19:00:00+08:00",
        )
        under_one_hour = make_input(
            "2026-10-10T13:01:00+08:00",
            "2026-10-11T19:00:00+08:00",
        )
        self.assertEqual(slot_map(one_hour.days[0])["lunch"].eligibility, "conditional")
        self.assertEqual(slot_map(under_one_hour.days[0])["lunch"].eligibility, "skipped")

    def test_sightseeing_window_is_not_subject_to_three_hour_minimum(self):
        result = make_input(
            "2026-10-10T09:00:00+08:00",
            "2026-10-11T16:00:00+08:00",
            explicit_return_buffer_s=0,
        )
        afternoon = slot_map(result.days[-1])["afternoon"]
        self.assertEqual(afternoon.eligibility, "conditional")
        self.assertIn("返程", afternoon.condition_reason)

    def test_verified_day_ready_at_replaces_arrival_upper_bound(self):
        result = make_input(
            "2026-10-10T09:00:00+08:00",
            "2026-10-12T19:00:00+08:00",
            day_ready_at="2026-10-10T14:30:00+08:00",
        )
        self.assertEqual(result.days[0].available_start.isoformat(), "2026-10-10T14:30:00+08:00")
        self.assertEqual(result.boundary_facts.day_ready_at.isoformat(), "2026-10-10T14:30:00+08:00")
        day1 = slot_map(result.days[0])
        self.assertEqual(day1["lunch"].eligibility, "skipped")
        self.assertEqual(day1["afternoon"].eligibility, "required")

    def test_return_upper_bound_at_meal_window_end_stays_conditional(self):
        result = make_input(
            "2026-10-10T09:00:00+08:00",
            "2026-10-12T15:00:00+08:00",
            explicit_return_buffer_s=3600,
        )
        last_day = slot_map(result.days[-1])
        self.assertEqual(last_day["breakfast"].eligibility, "required")
        self.assertEqual(last_day["morning"].eligibility, "required")
        self.assertEqual(last_day["lunch"].eligibility, "conditional")
        self.assertEqual(last_day["afternoon"].eligibility, "skipped")
        self.assertIn("返程", last_day["lunch"].condition_reason)

    def test_day_ready_at_must_fall_inside_trip(self):
        request = make_request("2026-10-10T09:00:00+08:00", "2026-10-12T19:00:00+08:00")
        with self.assertRaises(ValidationError):
            PlanningInputBuildRequest(
                request=request,
                requirements=make_summary(request),
                rules=make_rules(),
                day_ready_at=datetime.fromisoformat("2026-10-09T22:00:00+00:00"),
            )

    def test_candidate_return_duration_is_not_an_input_to_day_rules(self):
        request = make_request("2026-10-10T09:00:00+08:00", "2026-10-12T19:00:00+08:00")
        with self.assertRaises(ValidationError):
            PlanningInputBuildRequest(
                request=request,
                requirements=make_summary(request),
                rules=make_rules(),
                candidate_return_travel_s=6 * 3600,
            )


if __name__ == "__main__":
    unittest.main()
