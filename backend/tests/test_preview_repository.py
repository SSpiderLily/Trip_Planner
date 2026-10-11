"""独立预览草稿与最终保存仓库的 SQLite 回归测试。"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import contextmanager

from app.models.planning_v4.plan import ItineraryPreview
from app.models.preview_storage import (
    IdempotencyConflict,
    PreviewAccessError,
    PreviewConflict,
    PreviewIdentityConflict,
)
from app.services.preview_repository import PreviewRepository
from app.services.task_repository import TaskRepository
from test_planning_v4_contract import make_full_baseline_preview


@contextmanager
def database(path: Path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def baseline_preview() -> ItineraryPreview:
    return make_full_baseline_preview()[-1]


def changed_calculation(preview: ItineraryPreview, calculation_id: str) -> ItineraryPreview:
    value = preview.model_dump(mode="json")
    value["calculation"]["calculation_id"] = calculation_id
    return ItineraryPreview.model_validate(value)


def changed_plan_identity(preview: ItineraryPreview, plan_id: str) -> ItineraryPreview:
    value = preview.model_dump(mode="json")
    value["draft"]["plan_id"] = plan_id
    value["validation"]["plan_id"] = plan_id
    return ItineraryPreview.model_validate(value)


def revised_preview(preview: ItineraryPreview, revision: int, calculation_id: str) -> ItineraryPreview:
    value = preview.model_dump(mode="json")
    value["draft"]["revision"] = revision
    value["draft"]["base_revision"] = revision - 1
    for leg in value["draft"]["legs"]:
        leg["plan_revision"] = revision
    value["validation"]["based_on_revision"] = revision
    value["calculation"]["based_on_revision"] = revision
    value["calculation"]["calculation_id"] = calculation_id
    return ItineraryPreview.model_validate(value)


def changed_rule_identity(preview: ItineraryPreview) -> ItineraryPreview:
    value = preview.model_dump(mode="json")
    value["draft"]["rule_version"] = "other-rules"
    value["planning_input"]["rules"]["rule_version"] = "other-rules"
    return ItineraryPreview.model_validate(value)


def business_invalid_but_structural_preview(preview: ItineraryPreview) -> ItineraryPreview:
    value = preview.model_dump(mode="json")
    value["draft"]["days"][0]["activities"][0]["candidate_ref"] = "missing-candidate"
    return ItineraryPreview.model_validate(value)


class PreviewRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "preview.sqlite3"
        self.repo = PreviewRepository(self.path)
        self.repo.initialize()
        self.preview = baseline_preview()
        self.created = self.repo.create(self.preview)

    def tearDown(self):
        self.temp.cleanup()

    def test_draft_survives_repository_reopen_and_token_is_not_stored(self):
        reopened = PreviewRepository(self.path)
        reopened.initialize()
        record = reopened.get_preview(self.created.access_token)

        self.assertIsNotNone(record)
        self.assertEqual(record.store_version, 1)
        self.assertEqual(record.plan_revision, self.preview.draft.revision)
        self.assertEqual(record.preview, self.preview)
        with database(self.path) as conn:
            stored_hash = conn.execute(
                "SELECT access_token_hash FROM preview_drafts WHERE plan_id=?",
                (self.preview.draft.plan_id,),
            ).fetchone()[0]
        self.assertNotEqual(stored_hash, self.created.access_token)

    def test_invalid_token_does_not_reveal_preview_or_final(self):
        self.assertIsNone(self.repo.get_preview("wrong-token"))
        self.assertIsNone(self.repo.read_final("wrong-token"))
        with self.assertRaises(PreviewAccessError):
            self.repo.replace_preview(
                "wrong-token", self.preview, expected_store_version=1,
                expected_plan_revision=1, command_id="cmd-1",
            )

    def test_token_cannot_write_a_different_plan(self):
        second = self.repo.create(changed_plan_identity(self.preview, "plan-2"))
        with self.assertRaises(PreviewAccessError):
            self.repo.replace_preview(
                second.access_token, self.preview, expected_store_version=1,
                expected_plan_revision=1, command_id="cmd-1",
            )

    def test_command_replay_returns_current_snapshot_and_original_receipt_version(self):
        first_write = changed_calculation(self.preview, "calc-command-1")
        result = self.repo.replace_preview(
            self.created.access_token, first_write, expected_store_version=1,
            expected_plan_revision=1, command_id="command-1",
        )
        self.assertFalse(result.replayed)
        self.assertEqual(result.receipt_store_version, 2)

        second_write = changed_calculation(first_write, "calc-command-2")
        current = self.repo.replace_preview(
            self.created.access_token, second_write, expected_store_version=2,
            expected_plan_revision=1, command_id="command-2",
        )
        replay = self.repo.replace_preview(
            self.created.access_token, first_write, expected_store_version=1,
            expected_plan_revision=1, command_id="command-1",
        )

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt_store_version, 2)
        self.assertEqual(replay.record.store_version, 3)
        self.assertEqual(replay.record.preview, current.record.preview)
        self.assertEqual(replay.record.preview.calculation.calculation_id, "calc-command-2")

    def test_command_id_with_different_request_conflicts(self):
        first = changed_calculation(self.preview, "calc-one")
        self.repo.replace_preview(
            self.created.access_token, first, expected_store_version=1,
            expected_plan_revision=1, command_id="reused-id",
        )
        with self.assertRaises(IdempotencyConflict):
            self.repo.replace_preview(
                self.created.access_token, changed_calculation(self.preview, "calc-two"),
                expected_store_version=1, expected_plan_revision=1, command_id="reused-id",
            )

    def test_command_receipts_are_bounded_and_expired_replay_fails_cas(self):
        current = self.preview
        first_command = changed_calculation(current, "calc-1")
        self.repo.replace_preview(
            self.created.access_token, first_command, expected_store_version=1,
            expected_plan_revision=1, command_id="command-1",
        )
        current = first_command
        for index in range(2, 131):
            updated = changed_calculation(current, f"calc-{index}")
            self.repo.replace_preview(
                self.created.access_token, updated, expected_store_version=index,
                expected_plan_revision=1, command_id=f"command-{index}",
            )
            current = updated

        with database(self.path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM preview_mutations WHERE plan_id=?",
                (self.preview.draft.plan_id,),
            ).fetchone()[0]
        self.assertEqual(count, 128)
        with self.assertRaises(PreviewConflict):
            self.repo.replace_preview(
                self.created.access_token, first_command, expected_store_version=1,
                expected_plan_revision=1, command_id="command-1",
            )

    def test_plan_revision_and_store_version_are_independent(self):
        background = changed_calculation(self.preview, "calc-background")
        same_revision = self.repo.replace_preview(
            self.created.access_token, background, expected_store_version=1,
            expected_plan_revision=1, command_id="background-1",
        )
        self.assertEqual((same_revision.record.store_version, same_revision.record.plan_revision), (2, 1))

        edited = revised_preview(background, 2, "calc-after-edit")
        edit_result = self.repo.replace_preview(
            self.created.access_token, edited, expected_store_version=2,
            expected_plan_revision=1, command_id="edit-1",
        )
        self.assertEqual((edit_result.record.store_version, edit_result.record.plan_revision), (3, 2))

        late_route_result = changed_calculation(self.preview, "calc-late-route")
        with self.assertRaises(PreviewConflict):
            self.repo.replace_preview(
                self.created.access_token, late_route_result, expected_store_version=3,
                expected_plan_revision=1, command_id="late-route-1",
            )
        self.assertEqual(self.repo.get_preview(self.created.access_token).preview, edited)

    def test_identity_cannot_change_and_revision_cannot_go_backwards(self):
        with self.assertRaises(PreviewIdentityConflict):
            self.repo.replace_preview(
                self.created.access_token, changed_rule_identity(self.preview),
                expected_store_version=1, expected_plan_revision=1, command_id="identity-change",
            )
        edited = revised_preview(self.preview, 2, "calc-rev-2")
        self.repo.replace_preview(
            self.created.access_token, edited, expected_store_version=1,
            expected_plan_revision=1, command_id="edit-rev-2",
        )
        with self.assertRaises(PreviewConflict):
            self.repo.replace_preview(
                self.created.access_token, self.preview, expected_store_version=2,
                expected_plan_revision=2, command_id="revision-backwards",
            )

    def test_two_concurrent_writes_with_same_base_have_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def write(index: int) -> None:
            value = changed_calculation(self.preview, f"calc-race-{index}")
            barrier.wait(timeout=3)
            try:
                result = self.repo.replace_preview(
                    self.created.access_token, value, expected_store_version=1,
                    expected_plan_revision=1, command_id=f"race-{index}",
                )
                outcome = ("written", result.record.store_version)
            except PreviewConflict:
                outcome = ("conflict", None)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=write, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(value[0] for value in outcomes), ["conflict", "written"])
        self.assertEqual(self.repo.get_preview(self.created.access_token).store_version, 2)

    def test_final_is_updated_only_for_current_export_version_and_old_exports_cannot_overwrite(self):
        saved_v1 = self.repo.save_final(
            self.created.access_token, save_id="save-v1", source_store_version=1,
        )
        self.assertEqual(saved_v1.status, "saved")
        self.assertEqual(saved_v1.final.preview.draft.revision, 1)

        v2 = revised_preview(self.preview, 2, "calc-v2")
        updated = self.repo.replace_preview(
            self.created.access_token, v2, expected_store_version=1,
            expected_plan_revision=1, command_id="edit-v2",
        )
        stale_export = self.repo.save_final(
            self.created.access_token, save_id="late-save-v1", source_store_version=1,
        )
        self.assertEqual(stale_export.status, "stale")
        self.assertEqual(stale_export.final.save_id, "save-v1")

        saved_v2 = self.repo.save_final(
            self.created.access_token, save_id="save-v2", source_store_version=updated.record.store_version,
        )
        self.assertEqual(saved_v2.status, "saved")
        self.assertEqual(saved_v2.final.preview.draft.revision, 2)

        replay_late = self.repo.save_final(
            self.created.access_token, save_id="late-save-v1", source_store_version=1,
        )
        self.assertTrue(replay_late.replayed)
        self.assertEqual(replay_late.status, "stale")
        self.assertEqual(self.repo.read_final(self.created.access_token).save_id, "save-v2")

    def test_save_id_is_idempotent_and_rejects_different_source_version(self):
        saved = self.repo.save_final(
            self.created.access_token, save_id="save-once", source_store_version=1,
        )
        replay = self.repo.save_final(
            self.created.access_token, save_id="save-once", source_store_version=1,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.final, saved.final)
        with self.assertRaises(IdempotencyConflict):
            self.repo.save_final(
                self.created.access_token, save_id="save-once", source_store_version=2,
            )

    def test_final_write_transaction_failure_preserves_previous_final(self):
        first = self.repo.save_final(
            self.created.access_token, save_id="save-before-failure", source_store_version=1,
        )
        v2 = revised_preview(self.preview, 2, "calc-v2-failure")
        self.repo.replace_preview(
            self.created.access_token, v2, expected_store_version=1,
            expected_plan_revision=1, command_id="edit-before-failure",
        )
        with database(self.path) as conn:
            conn.execute(
                """CREATE TRIGGER fail_final_update BEFORE UPDATE ON preview_finals
                   BEGIN SELECT RAISE(ABORT, 'injected final save failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.save_final(
                self.created.access_token, save_id="save-fails", source_store_version=2,
            )
        with database(self.path) as conn:
            conn.execute("DROP TRIGGER fail_final_update")
        self.assertEqual(self.repo.read_final(self.created.access_token), first.final)
        with database(self.path) as conn:
            receipt = conn.execute(
                "SELECT 1 FROM preview_mutations WHERE plan_id=? AND mutation_kind='save' AND mutation_id='save-fails'",
                (self.preview.draft.plan_id,),
            ).fetchone()
        self.assertIsNone(receipt)

    def test_final_can_be_read_for_restore_while_draft_remains_editable(self):
        self.repo.save_final(self.created.access_token, save_id="save-v1", source_store_version=1)
        restored_source = self.repo.read_final(self.created.access_token)
        self.assertEqual(restored_source.preview, self.preview)

        edited = revised_preview(restored_source.preview, 2, "calc-restored-edit")
        result = self.repo.replace_preview(
            self.created.access_token, edited, expected_store_version=1,
            expected_plan_revision=1, command_id="edit-after-restore",
        )
        self.assertEqual(result.record.preview.draft.revision, 2)
        self.assertEqual(self.repo.read_final(self.created.access_token).preview.draft.revision, 1)

    def test_observation_cleanup_cannot_cascade_into_preview_storage(self):
        tasks = TaskRepository(self.path, retention_days=0)
        tasks.initialize()
        tasks.create("observation-task", {})
        tasks.start("observation-task")
        tasks.succeed("observation-task", {"status": "done"})
        with database(self.path) as conn:
            conn.execute(
                "UPDATE tasks SET finished_at='2000-01-01T00:00:00+00:00' WHERE task_id='observation-task'"
            )
        tasks.cleanup()

        self.assertIsNone(tasks.status("observation-task"))
        self.assertEqual(self.repo.get_preview(self.created.access_token).preview, self.preview)
        self.assertIsNone(self.repo.read_final(self.created.access_token))
        with database(self.path) as conn:
            for table in ("preview_drafts", "preview_finals", "preview_mutations"):
                foreign_keys = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
                self.assertEqual(foreign_keys, [])

    def test_repository_checks_model_structure_without_business_reference_validation(self):
        from app.services.planning_v4_validator import ContractValidationError, validate_preview

        preview = business_invalid_but_structural_preview(self.preview)
        with self.assertRaises(ContractValidationError):
            validate_preview(preview)
        self.repo.replace_preview(
            self.created.access_token, preview, expected_store_version=1,
            expected_plan_revision=1, command_id="business-invalid-but-structured",
        )
        self.assertEqual(self.repo.get_preview(self.created.access_token).preview, preview)


if __name__ == "__main__":
    unittest.main()
