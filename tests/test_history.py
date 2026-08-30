from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest

from kilix_image_shop.domain.commands import (
    ResolvedObject,
    SetLayerProperty,
    reduce_command,
)
from kilix_image_shop.domain.identifiers import RevisionId
from kilix_image_shop.history.budget import HistoryBudget, HistoryBudgetError
from kilix_image_shop.history.spill import SpillError, SpillStore
from kilix_image_shop.history.stack import (
    RESTORE_CONTROLS,
    HistoryError,
    HistoryRecord,
    HistoryStack,
    RedoUnavailable,
    UndoUnavailable,
    command_bytes,
)

from domain_fixtures import layer_id, object_id, sample_document


class TogglePutSpillStore(SpillStore):
    __slots__ = ("fail_put",)

    def put(self, entry_id, payload):
        if self.fail_put:
            raise SpillError("synthetic spill publication failure")
        return super().put(entry_id, payload)


class FailSecondPutSpillStore(SpillStore):
    __slots__ = ("put_calls",)

    def put(self, entry_id, payload):
        object.__setattr__(self, "put_calls", self.put_calls + 1)
        if self.put_calls == 2:
            raise SpillError("synthetic second spill publication failure")
        return super().put(entry_id, payload)


def revision(number: int) -> RevisionId:
    return RevisionId(f"00000000-0000-4000-8000-{number:012d}")


def command_for(stack: HistoryStack, number: int, name: str) -> SetLayerProperty:
    return SetLayerProperty(
        expected_revision=stack.current_state.revision_id,
        new_revision=revision(number),
        layer_id=layer_id(1),
        name=name,
    )


class HistoryBudgetTests(unittest.TestCase):
    def test_all_three_budget_values_are_required_finite_and_positive(self) -> None:
        self.assertEqual(
            HistoryBudget(8, 4096, 8192),
            HistoryBudget(
                max_entries=8,
                max_resident_bytes=4096,
                max_spill_bytes=8192,
            ),
        )
        malformed = (
            (0, 1, 1),
            (1, 0, 1),
            (1, 1, 0),
            (True, 1, 1),
            (1, 1.0, 1),
        )
        for values in malformed:
            with self.subTest(values=values), self.assertRaises(HistoryBudgetError):
                HistoryBudget(*values)

    def test_spill_root_must_be_explicit_owner_private_xdg_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            root.chmod(0o755)
            with self.assertRaises(SpillError):
                SpillStore.create(
                    root,
                    sample_document().document_id,
                    max_record_bytes=8192,
                )


class HistoryRestoreTests(unittest.TestCase):
    def make_stack(
        self,
        temporary: str,
        budget: HistoryBudget = HistoryBudget(8, 1_000_000, 1_000_000),
    ) -> tuple[HistoryStack, SpillStore]:
        spill = SpillStore.create(
            pathlib.Path(temporary),
            sample_document().document_id,
            max_record_bytes=budget.max_spill_bytes,
        )
        return HistoryStack(sample_document(), budget, spill), spill

    def append(
        self,
        stack: HistoryStack,
        number: int,
        name: str,
        *,
        before_objects: tuple[ResolvedObject, ...] = (),
        after_objects: tuple[ResolvedObject, ...] = (),
    ) -> None:
        command = command_for(stack, number, name)
        result = reduce_command(stack.current_state, command)
        stack.record(
            command,
            result,
            before_objects=before_objects,
            after_objects=after_objects,
        )

    def test_undo_and_redo_apply_all_four_controls_and_publish_one_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stack, _ = self.make_stack(temporary)
            reference = ResolvedObject(object_id("6"), 128)
            self.append(
                stack,
                3,
                "renamed",
                before_objects=(reference,),
                after_objects=(reference,),
            )
            after = stack.current_state
            validated: list[ResolvedObject] = []

            def validate(item: ResolvedObject) -> bool:
                validated.append(item)
                return True

            undo = stack.undo(
                expected_revision=after.revision_id,
                new_revision=revision(100),
                object_validator=validate,
            )
            self.assertEqual(undo.controls, RESTORE_CONTROLS)
            self.assertEqual(len(undo.controls), 4)
            self.assertEqual(undo.invalidated_layer_ids, (layer_id(1),))
            self.assertEqual(stack.current_state.revision_id, revision(100))
            self.assertEqual(stack.current_state.layer_map[layer_id(1)].name, "Pixels")
            redo = stack.redo(
                expected_revision=revision(100),
                new_revision=revision(101),
                object_validator=validate,
            )
            self.assertEqual(redo.controls, RESTORE_CONTROLS)
            self.assertEqual(stack.current_state.revision_id, revision(101))
            self.assertEqual(stack.current_state.layer_map[layer_id(1)].name, "renamed")
            self.assertEqual(validated, [reference, reference])

    def test_failed_object_validation_changes_zero_document_states_or_cursors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stack, _ = self.make_stack(temporary)
            reference = ResolvedObject(object_id("6"), 128)
            self.append(stack, 3, "renamed", before_objects=(reference,))
            before = stack.current_state
            before_usage = stack.usage
            with self.assertRaises(HistoryError):
                stack.undo(
                    expected_revision=before.revision_id,
                    new_revision=revision(100),
                    object_validator=lambda item: False,
                )
            self.assertIs(stack.current_state, before)
            self.assertEqual(stack.usage, before_usage)
            self.assertTrue(stack.can_undo)
            self.assertFalse(stack.can_redo)

    def test_failed_spill_publication_restores_entries_cursor_and_redo_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = SpillStore.create(
                pathlib.Path(temporary),
                sample_document().document_id,
                max_record_bytes=1_000_000,
            )
            spill = TogglePutSpillStore(base.root, base.max_record_bytes)
            object.__setattr__(spill, "fail_put", False)
            stack = HistoryStack(
                sample_document(),
                HistoryBudget(8, 1, 1_000_000),
                spill,
            )
            self.append(stack, 3, "first")
            stack.undo(
                expected_revision=revision(3),
                new_revision=revision(100),
                object_validator=lambda item: True,
            )
            before = stack.current_state
            before_usage = stack.usage
            command = command_for(stack, 101, "replacement")
            result = reduce_command(stack.current_state, command)
            object.__setattr__(spill, "fail_put", True)
            with self.assertRaises(SpillError):
                stack.record(command, result)
            self.assertIs(stack.current_state, before)
            self.assertEqual(stack.usage, before_usage)
            self.assertTrue(stack.can_redo)
            object.__setattr__(spill, "fail_put", False)
            restored = stack.redo(
                expected_revision=revision(100),
                new_revision=revision(102),
                object_validator=lambda item: True,
            )
            self.assertEqual(restored.state.layer_map[layer_id(1)].name, "first")

    def test_failed_later_spill_removes_newly_created_then_pruned_carrier(self) -> None:
        initial = sample_document()
        first_command = SetLayerProperty(
            expected_revision=initial.revision_id,
            new_revision=revision(3),
            layer_id=layer_id(1),
            name="a",
        )
        first_result = reduce_command(initial, first_command)
        first_record = HistoryRecord(
            command_bytes(first_command),
            initial,
            first_result.state,
            (),
            (),
            first_result.changed_layer_ids,
        )
        second_command = SetLayerProperty(
            expected_revision=first_result.state.revision_id,
            new_revision=revision(4),
            layer_id=layer_id(1),
            name="x" * 100,
        )
        second_result = reduce_command(first_result.state, second_command)
        second_record = HistoryRecord(
            command_bytes(second_command),
            first_result.state,
            second_result.state,
            (),
            (),
            second_result.changed_layer_ids,
        )
        self.assertGreater(second_record.resident_bytes, first_record.resident_bytes)
        spill_limit = max(
            len(first_record.canonical_bytes()),
            len(second_record.canonical_bytes()),
        )
        budget = HistoryBudget(8, first_record.resident_bytes, spill_limit)
        with tempfile.TemporaryDirectory() as temporary:
            base = SpillStore.create(
                pathlib.Path(temporary),
                initial.document_id,
                max_record_bytes=spill_limit,
            )
            spill = FailSecondPutSpillStore(base.root, base.max_record_bytes)
            object.__setattr__(spill, "put_calls", 0)
            stack = HistoryStack(initial, budget, spill)
            stack.record(first_command, first_result)
            before = stack.current_state
            before_usage = stack.usage
            with self.assertRaises(SpillError):
                stack.record(second_command, second_result)
            self.assertIs(stack.current_state, before)
            self.assertEqual(stack.usage, before_usage)
            self.assertEqual(tuple(spill.root.glob("*.json")), ())

    def test_missing_or_corrupt_spill_fails_visibly_without_state_change(self) -> None:
        for corruption in ("missing", "digest"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                budget = HistoryBudget(8, 1, 1_000_000)
                stack, spill = self.make_stack(temporary, budget)
                self.append(stack, 3, "renamed")
                self.assertEqual(stack.usage.resident_bytes, 0)
                self.assertGreater(stack.usage.spill_bytes, 0)
                carrier = next(spill.root.glob("*.json"))
                if corruption == "missing":
                    carrier.unlink()
                else:
                    carrier.write_bytes(b"x" * carrier.stat().st_size)
                before = stack.current_state
                usage = stack.usage
                with self.assertRaises(HistoryError):
                    stack.undo(
                        expected_revision=before.revision_id,
                        new_revision=revision(100),
                        object_validator=lambda item: True,
                    )
                self.assertIs(stack.current_state, before)
                self.assertEqual(stack.usage, usage)

    def test_new_edit_discards_redo_metadata_and_releases_its_spill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            budget = HistoryBudget(8, 1, 1_000_000)
            stack, spill = self.make_stack(temporary, budget)
            self.append(stack, 3, "first")
            stack.undo(
                expected_revision=revision(3),
                new_revision=revision(100),
                object_validator=lambda item: True,
            )
            self.assertTrue(stack.can_redo)
            self.assertEqual(len(tuple(spill.root.glob("*.json"))), 1)
            self.append(stack, 101, "replacement")
            self.assertFalse(stack.can_redo)
            with self.assertRaises(RedoUnavailable):
                stack.redo(
                    expected_revision=revision(101),
                    new_revision=revision(102),
                    object_validator=lambda item: True,
                )
            self.assertEqual(len(tuple(spill.root.glob("*.json"))), 1)

    def test_entry_ceiling_prunes_oldest_boundary_and_reports_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stack, _ = self.make_stack(
                temporary,
                HistoryBudget(2, 1_000_000, 1_000_000),
            )
            self.append(stack, 3, "one")
            self.append(stack, 4, "two")
            self.append(stack, 5, "three")
            self.append(stack, 6, "four")
            self.assertEqual(stack.usage.entries, 2)
            self.assertEqual(stack.usage.pruned_entries, 2)
            stack.undo(
                expected_revision=revision(6),
                new_revision=revision(100),
                object_validator=lambda item: True,
            )
            stack.undo(
                expected_revision=revision(100),
                new_revision=revision(101),
                object_validator=lambda item: True,
            )
            with self.assertRaises(UndoUnavailable):
                stack.undo(
                    expected_revision=revision(101),
                    new_revision=revision(102),
                    object_validator=lambda item: True,
                )

    def test_spill_ceiling_prunes_until_both_byte_counters_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            budget = HistoryBudget(20, 1, 22_000)
            stack, _ = self.make_stack(temporary, budget)
            for number in range(3, 13):
                self.append(stack, number, f"name-{number}")
            usage = stack.usage
            self.assertLessEqual(usage.resident_bytes, budget.max_resident_bytes)
            self.assertLessEqual(usage.spill_bytes, budget.max_spill_bytes)
            self.assertGreater(usage.pruned_entries, 0)
            self.assertEqual(usage.entries, usage.undoable_entries)

    def test_revisions_are_never_reused_across_edit_undo_or_redo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stack, _ = self.make_stack(temporary)
            self.append(stack, 3, "first")
            stack.undo(
                expected_revision=revision(3),
                new_revision=revision(100),
                object_validator=lambda item: True,
            )
            with self.assertRaises(HistoryError):
                stack.redo(
                    expected_revision=revision(100),
                    new_revision=revision(3),
                    object_validator=lambda item: True,
                )

    def test_retained_reachability_includes_both_old_and_new_object_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stack, _ = self.make_stack(temporary)
            before = ResolvedObject(object_id("6"), 128)
            after = ResolvedObject(object_id("7"), 96)
            self.append(
                stack,
                3,
                "changed",
                before_objects=(before,),
                after_objects=(after,),
            )
            self.assertEqual(
                stack.reachable_object_ids(),
                (before.object_id, after.object_id),
            )


class HistoryDependencyTests(unittest.TestCase):
    def test_three_history_modules_store_metadata_and_import_zero_render_engines(self) -> None:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "history"
        )
        modules = tuple(
            sorted(path.name for path in root.glob("*.py") if path.name != "__init__.py")
        )
        self.assertEqual(modules, ("budget.py", "spill.py", "stack.py"))
        forbidden: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                forbidden.extend(
                    name
                    for name in names
                    if name.startswith(
                        (
                            "gi",
                            "kilix_image_shop.engine",
                            "kilix_image_shop.render",
                        )
                    )
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
