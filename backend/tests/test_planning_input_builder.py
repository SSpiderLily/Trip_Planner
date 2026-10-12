from datetime import date, datetime
import unittest

from pydantic import ValidationError

from app.models.planning_v4 import PlanningInput, RequirementSummary, RuleSet, TripRequest
from app.services.planning_input_builder import (
    PlanningInputBuildRequest,
    build_planning_input,
)
from app.services.planning_v4_rules import default_planning_v4_rules


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
        "meal_minimum_available_s": 3600,
        "sightseeing_minimum_available_s": 10800,
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
):
    request = make_request(arrival, departure)
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=make_summary(request),
        rules=rules or make_rules(),
        explicit_return_buffer_s=explicit_return_buffer_s,
    ))


def slot_map(day):
    return {slot.period: slot for slot in day.slots}


class PlanningInputBuilderTests(unittest.TestCase):
    def test_request_return_buffer_zero_is_distinct_from_default(self):
        default_request = make_request(
            "2026-10-10T09:00:00+08:00", "2026-10-12T19:00:00+08:00",
        )
        explicit_request = default_request.model_copy(update={"explicit_return_buffer_s": 0})

        defaulted = build_planning_input(PlanningInputBuildRequest(
            request=default_request,
            requirements=make_summary(default_request),
            rules=make_rules(),
        ))
        explicit_zero = build_planning_input(PlanningInputBuildRequest(
            request=explicit_request,
            requirements=make_summary(explicit_request),
            rules=make_rules(),
        ))

        self.assertEqual(defaulted.boundary_facts.effective_return_buffer_s, 3600)
        self.assertEqual(defaulted.boundary_facts.return_buffer_source, "default")
        self.assertEqual(explicit_zero.boundary_facts.effective_return_buffer_s, 0)
        self.assertEqual(explicit_zero.boundary_facts.return_buffer_source, "explicit_user")

    def test_shanghai_three_day_reference(self):
        result = make_input()

        self.assertEqual(result.request.request_id, result.requirements.request_id)
        self.assertEqual([day.day_date for day in result.days], [
            date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12),
        ])
        self.assertEqual([len(day.slots) for day in result.days], [6, 6, 6])
        self.assertEqual([slot.period for slot in result.days[0].slots], list(PERIODS))
        self.assertEqual(result.days[0].available_start.isoformat(), "2026-10-10T09:00:00+08:00")
        self.assertEqual(result.days[1].available_start.isoformat(), "2026-10-11T08:00:00+08:00")
        self.assertEqual(result.days[2].available_end.isoformat(), "2026-10-12T18:00:00+08:00")
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
                "lunch": "required",
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
                "dinner": "required",
                "evening": "skipped",
            },
        )
        self.assertTrue(all(slot.condition_reason is None for day in result.days for slot in day.slots))
        self.assertIsNone(result.boundary_facts.day_ready_at)

    def test_confirmed_october_11_to_13_example_uses_production_rules(self):
        result = make_input(
            "2026-10-11T08:00:00+08:00",
            "2026-10-13T18:00:00+08:00",
            rules=default_planning_v4_rules(),
        )
        self.assertEqual([
            tuple(slot.period for slot in day.slots if slot.eligibility == "required")
            for day in result.days
        ], [
            ("breakfast", "lunch", "afternoon", "dinner", "evening"),
            PERIODS,
            ("breakfast", "morning", "lunch", "afternoon"),
        ])
        self.assertEqual(result.days[-1].available_end.isoformat(), "2026-10-13T17:00:00+08:00")

    def test_arrival_boundaries_are_half_open(self):
        cases = (
            ("00:00:00", ("skipped", "required", "required")),
            ("05:59:59", ("skipped", "required", "required")),
            ("06:00:00", ("skipped", "required", "required")),
            ("11:59:59", ("skipped", "required", "required")),
            ("12:00:00", ("skipped", "skipped", "required")),
            ("17:59:59", ("skipped", "skipped", "required")),
            ("18:00:00", ("skipped", "skipped", "skipped")),
            ("23:59:59", ("skipped", "skipped", "skipped")),
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
                self.assertEqual(slot_map(result.days[1])["morning"].eligibility, "required")

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
        self.assertEqual(result.days[0].available_start.isoformat(), "2026-10-10T06:00:00+08:00")
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
        self.assertEqual(breakfast.eligibility, "required")
        self.assertIsNone(breakfast.condition_reason)

        below_three_hours = make_input(
            "2026-10-10T08:00:00+08:00",
            "2026-10-10T11:59:59+08:00",
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
        self.assertEqual(slot_map(defaulted.days[0])["breakfast"].eligibility, "required")
        self.assertEqual({slot.eligibility for slot in explicit_two_hours.days[0].slots}, {"skipped"})
        self.assertEqual(explicit_two_hours.boundary_facts.effective_return_buffer_s, 7200)
        self.assertEqual(explicit_two_hours.boundary_facts.return_buffer_source, "explicit_user")

        restored = PlanningInput.model_validate_json(explicit_two_hours.model_dump_json())
        self.assertEqual(restored.boundary_facts, explicit_two_hours.boundary_facts)

    def test_arrival_meals_require_at_least_one_hour_independently_of_sightseeing(self):
        for period, clock, expected in (
            ("breakfast", "08:00:00", "required"),
            ("breakfast", "08:00:01", "skipped"),
            ("lunch", "13:00:00", "required"),
            ("lunch", "13:00:01", "skipped"),
            ("dinner", "18:00:00", "required"),
            ("dinner", "18:00:01", "skipped"),
        ):
            with self.subTest(period=period, clock=clock):
                result = make_input(f"2026-10-10T{clock}+08:00")
                self.assertEqual(slot_map(result.days[0])[period].eligibility, expected)

    def test_departure_windows_require_full_sightseeing_block_or_one_hour_meal(self):
        for period, departure, expected in (
            ("morning", "13:00:00", "required"),
            ("morning", "12:59:59", "skipped"),
            ("afternoon", "18:00:00", "required"),
            ("afternoon", "17:59:59", "skipped"),
            ("evening", "23:00:00", "required"),
            ("evening", "22:59:59", "skipped"),
            ("lunch", "14:00:00", "required"),
            ("lunch", "13:59:59", "skipped"),
            ("dinner", "19:00:00", "required"),
            ("dinner", "18:59:59", "skipped"),
        ):
            with self.subTest(period=period, departure=departure):
                result = make_input(departure=f"2026-10-12T{departure}+08:00")
                self.assertEqual(slot_map(result.days[-1])[period].eligibility, expected)

    def test_departure_day_under_three_hours_skips_all_slots_but_exact_three_hours_keeps_breakfast(self):
        for departure, expected in (("11:59:59", "skipped"), ("12:00:00", "required")):
            with self.subTest(departure=departure):
                result = make_input(departure=f"2026-10-12T{departure}+08:00")
                day = result.days[-1]
                self.assertEqual(slot_map(day)["breakfast"].eligibility, expected)
                self.assertEqual({slot.eligibility for slot in day.slots[1:]}, {"skipped"})

    def test_return_cutoff_at_meal_window_end_is_required(self):
        result = make_input(
            "2026-10-10T09:00:00+08:00",
            "2026-10-12T15:00:00+08:00",
            explicit_return_buffer_s=3600,
        )
        last_day = slot_map(result.days[-1])
        self.assertEqual(last_day["breakfast"].eligibility, "required")
        self.assertEqual(last_day["morning"].eligibility, "required")
        self.assertEqual(last_day["lunch"].eligibility, "required")
        self.assertEqual(last_day["afternoon"].eligibility, "skipped")
        self.assertIsNone(last_day["lunch"].condition_reason)

    def test_day_ready_at_is_not_an_input_to_slot_rules(self):
        request = make_request("2026-10-10T09:00:00+08:00", "2026-10-12T19:00:00+08:00")
        with self.assertRaises(ValidationError):
            PlanningInputBuildRequest(
                request=request,
                requirements=make_summary(request),
                rules=make_rules(),
                day_ready_at=datetime.fromisoformat("2026-10-10T14:30:00+08:00"),
            )

    def test_buffer_covering_available_day_keeps_six_skipped_slots(self):
        for buffer_s in (4 * 3600, 5 * 3600, 24 * 3600):
            with self.subTest(buffer_s=buffer_s):
                result = make_input(
                    "2026-10-10T08:00:00+08:00", "2026-10-10T12:00:00+08:00",
                    explicit_return_buffer_s=buffer_s,
                )
                self.assertEqual(len(result.days[0].slots), 6)
                self.assertEqual({slot.eligibility for slot in result.days[0].slots}, {"skipped"})
                self.assertIsNone(result.days[0].available_end)
                self.assertEqual(result.boundary_facts.effective_return_buffer_s, buffer_s)
                self.assertEqual(PlanningInput.model_validate_json(result.model_dump_json()), result)

    def test_explicit_build_buffer_takes_precedence_over_request_buffer(self):
        request = make_request("2026-10-10T08:00:00+08:00", "2026-10-10T12:00:00+08:00")
        request = request.model_copy(update={"explicit_return_buffer_s": 7200})
        result = build_planning_input(PlanningInputBuildRequest(
            request=request, requirements=make_summary(request), rules=make_rules(),
            explicit_return_buffer_s=0,
        ))
        self.assertEqual(result.boundary_facts.effective_return_buffer_s, 0)
        self.assertEqual(slot_map(result.days[0])["breakfast"].eligibility, "required")

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
