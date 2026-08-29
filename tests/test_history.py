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
    HistoryStack,
    RedoUnavailable,
    UndoUnavailable,
)

from domain_fixtures import layer_id, object_id, sample_document


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
