"""S03 时段地点契约、无 timing 快照与交通摘要的离线回归。"""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from pydantic import ValidationError

from app.agents.daily_plan_activity_repair import ActivityRepairOutput, DailyPlanModelOutput
from app.agents.daily_plan_generator import DailyDayRevisionModelOutput
from app.models.planning_v4 import DraftActivity, LegEndpoint, RouteLeg, RuleSet
from app.models.planning_v4_preview import PreviewSnapshot
from app.models.preview_storage import PreviewSnapshotUnsupported
from app.services.planning_v4_validation import build_day_summaries
from app.services.preview_repository import PreviewRepository
from test_planning_v4_contract import make_full_baseline_preview
from test_preview_repository import baseline_snapshot, revised_snapshot


class SlotPlanContractTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = baseline_snapshot()

    def test_initial_revision_and_local_repair_schemas_have_no_activity_time_fields(self):
        forbidden = {"duration_s", "duration_source", "duration_reason", "start_at", "end_at",
                     "opening_hours", "opening_hours_raw"}
        for model in (DailyPlanModelOutput, DailyDayRevisionModelOutput, ActivityRepairOutput):
            with self.subTest(model=model.__name__):
                for definition in model.model_json_schema()["$defs"].values():
                    self.assertFalse(forbidden & definition.get("properties", {}).keys())

    def test_snapshot_roundtrip_preserves_theme_slots_order_sources_without_timing(self):
        value = self.snapshot.model_dump(mode="json")
        value["preview"]["draft"]["days"][0]["theme"] = "黄浦江沿线"
        roundtrip = PreviewSnapshot.model_validate_json(json.dumps(value))
        self.assertEqual(roundtrip.model_dump(mode="json"), value)
        self.assertEqual(roundtrip.preview.draft.days[0].activities,
                         self.snapshot.preview.draft.days[0].activities)
        self.assertNotIn("timing", value)
        self.assertFalse({"duration_s", "duration_source", "duration_reason", "start_at", "end_at"}
                         & DraftActivity.model_fields.keys())

    def test_legacy_fields_are_rejected_without_conversion(self):
        original = self.snapshot.model_dump(mode="json")
        mutations = (
            lambda value: value.update(timing={}),
            lambda value: value["preview"]["draft"]["days"][0]["activities"][0].update(duration_s=3600),
            lambda value: value["preview"]["draft"]["days"][0]["activities"][0].update(start_at="09:00"),
            lambda value: value["preview"]["planning_input"]["rules"].update(meal_default_s=3600),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = deepcopy(original)
                mutate(value)
                before = deepcopy(value)
                with self.assertRaises(ValidationError):
                    PreviewSnapshot.model_validate(value)
                self.assertEqual(value, before)

    def test_snapshot_rejects_broken_references_sources_and_versions(self):
        original = self.snapshot.model_dump(mode="json")
        mutations = (
            lambda value: value["preview"]["draft"]["days"][0]["activities"][0].update(candidate_ref="missing"),
            lambda value: value["preview"]["candidates"][0].update(place_fact_id="missing"),
            lambda value: value["preview"]["facts"]["places"][0].update(source_refs=["missing"]),
            lambda value: value["preview"]["draft"]["legs"][0].update(selected_option_id="missing"),
            lambda value: value["preview"]["draft"]["legs"][0].update(plan_revision=2),
            lambda value: value["cost"].update(based_on_revision=2),
            lambda value: value["cost"]["days"].pop(),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = deepcopy(original)
                mutate(value)
                with self.assertRaises(ValidationError):
                    PreviewSnapshot.model_validate(value)

    def test_rules_are_eligibility_thresholds_without_default_stay_durations(self):
        rules = self.snapshot.preview.planning_input.rules
        self.assertEqual(rules.meal_minimum_available_s, 3600)
        self.assertEqual(rules.sightseeing_minimum_available_s, 10800)
        self.assertNotIn("sightseeing_duration_adjustable", RuleSet.model_fields)


class SlotTransportSummaryTest(unittest.TestCase):
    def setUp(self):
        self.planning_input, self.facts, _, _, self.preview = make_full_baseline_preview()

    def summary_with_duration(self, duration):
        route = self.facts.routes[0]
        unused = route.options[0].model_copy(update={"option_id": "unused-fast", "duration_s": 1})
        facts = self.facts.model_copy(update={"routes": (route.model_copy(update={
            "options": (route.options[0].model_copy(update={"duration_s": duration}), unused),
        }),)})
        return build_day_summaries(self.planning_input, self.preview.draft, facts)[1]

    def test_selected_duration_does_not_use_a_faster_unselected_option(self):
        summary = self.summary_with_duration(780)
        self.assertEqual(summary.known_travel_s, 780)
        self.assertEqual(summary.unknown_leg_ids, ())

    def test_missing_duration_remains_unknown_without_a_default(self):
        summary = self.summary_with_duration(None)
        self.assertEqual(summary.known_travel_s, 0)
        self.assertEqual(summary.unknown_leg_ids, (self.preview.draft.legs[0].leg_id,))

    def test_fractional_actual_duration_is_preserved(self):
        self.assertEqual(self.summary_with_duration(780.5).known_travel_s, 780.5)

    def test_same_place_transition_has_no_unknown_route(self):
        day = self.preview.draft.days[0]
        first, second = (item for item in day.activities if item.poi_id == "synthetic-poi-bund")
        leg = RouteLeg(
            leg_id="same-place-leg", plan_revision=1, day_id=day.day_id,
            from_endpoint=LegEndpoint(kind="activity", reference_id=first.activity_id, poi_id=first.poi_id),
            to_endpoint=LegEndpoint(kind="activity", reference_id=second.activity_id, poi_id=second.poi_id),
            selection_source="same_place",
        )
        draft = self.preview.draft.model_copy(update={"legs": (*self.preview.draft.legs, leg)})
        summary = build_day_summaries(self.planning_input, draft, self.facts)[0]
        self.assertEqual((summary.known_travel_s, summary.unknown_leg_ids), (0, ()))


class SlotSnapshotStorageTest(unittest.TestCase):
    def test_create_read_edit_save_reopen_restore_without_timing_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "previews.sqlite3"
            repository = PreviewRepository(path)
            repository.initialize()
            original = baseline_snapshot()
            created = repository.create(original)
            self.assertEqual(repository.get_preview(created.access_token).snapshot, original)
            repository.save_final(created.access_token, save_id="slot-original", source_store_version=1)
            revised = revised_snapshot(original, 2, "slot-edit")
            repository.replace_preview(created.access_token, revised, expected_store_version=1,
                                       expected_plan_revision=1, command_id="slot-edit")
            reopened = PreviewRepository(path)
            reopened.initialize()
            self.assertEqual(reopened.get_preview(created.access_token).snapshot, revised)
            self.assertEqual(reopened.read_final(created.access_token).snapshot, original)
            restored = reopened.restore_final(created.access_token, restore_id="slot-restore",
                                              expected_store_version=2, expected_plan_revision=2,
                                              expected_save_id="slot-original")
            self.assertEqual(restored.record.plan_revision, 3)
            self.assertEqual(restored.record.snapshot.preview.draft.days, original.preview.draft.days)
            self.assertNotIn("timing", restored.record.snapshot.model_dump())
            with sqlite3.connect(path) as connection:
                for table in ("preview_drafts", "preview_finals"):
                    fields = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    self.assertNotIn("timing_json", fields)

    def test_stored_old_activity_contract_is_rejected_without_rewriting_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "previews.sqlite3"
            repository = PreviewRepository(path)
            repository.initialize()
            created = repository.create(baseline_snapshot())
            with sqlite3.connect(path) as connection:
                raw = connection.execute("SELECT preview_json FROM preview_drafts").fetchone()[0]
                value = json.loads(raw)
                value["draft"]["days"][0]["activities"][0]["duration_s"] = 3600
                old_json = json.dumps(value)
                connection.execute("UPDATE preview_drafts SET preview_json=?", (old_json,))
            with self.assertRaises(PreviewSnapshotUnsupported):
                repository.get_preview(created.access_token)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT preview_json FROM preview_drafts").fetchone()[0], old_json)


if __name__ == "__main__":
    unittest.main()
