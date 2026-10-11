from __future__ import annotations

import unittest

from app.services.planning_v4_rules import (
    PLANNING_V4_RULE_VERSION,
    default_planning_v4_rules,
)


class PlanningV4RulesTests(unittest.TestCase):
    def test_production_rules_use_confirmed_windows_and_existing_defaults(self):
        rules = default_planning_v4_rules()

        self.assertEqual(rules.rule_version, PLANNING_V4_RULE_VERSION)
        self.assertEqual(
            tuple((item.period, item.start_minute, item.end_minute) for item in rules.windows),
            (
                ("breakfast", 480, 540),
                ("morning", 540, 720),
                ("lunch", 720, 840),
                ("afternoon", 840, 1020),
                ("dinner", 1020, 1140),
                ("evening", 1140, 1320),
            ),
        )
        self.assertEqual(rules.meal_default_s, 3600)
        self.assertEqual(rules.sightseeing_default_s, 10800)
        self.assertTrue(rules.sightseeing_duration_adjustable)
        self.assertEqual(rules.max_repair_rounds_per_day, 3)
        self.assertEqual(rules.full_day_start_minute, 480)
        self.assertEqual(rules.return_buffer_default_s, 3600)
        self.assertEqual(rules.departure_day_minimum_usable_s, 10800)
        self.assertEqual(rules.return_buffer_priority, "explicit_user_value_then_default")

    def test_rule_factory_returns_independent_immutable_models(self):
        first = default_planning_v4_rules()
        second = default_planning_v4_rules()

        self.assertEqual(first, second)
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
