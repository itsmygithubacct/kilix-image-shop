from __future__ import annotations

import ast
import dataclasses
import pathlib
import tempfile
import unittest

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import PixelLayer
from kilix_image_shop.store.gc import apply_gc, preview_gc, retire_quarantine
from kilix_image_shop.store.generations import (
    SAVE_POINT_COUNT,
    GenerationStore,
    SaveConflict,
    create_project,
)
from kilix_image_shop.store.layout import ProjectLayout, ProjectLimits, StoreError
from kilix_image_shop.store.locking import ProjectWriterLock
from kilix_image_shop.store.objects import ObjectRecord, ObjectStore
from kilix_image_shop.store.recovery import (
    OPEN_VALIDATION_CLASSES,
    OpenValidationError,
    apply_recovery,
    list_recovery_candidates,
    open_project,
    preview_recovery,
)

from domain_fixtures import empty_document


LIMITS = ProjectLimits(
    max_manifest_bytes=1_048_576,
    max_objects=128,
    max_object_bytes=1_048_576,
    max_total_object_bytes=8_388_608,
    max_layers=128,
    max_group_depth=16,
)


class InjectedFailure(RuntimeError):
    pass


def revised(number: int):
    return dataclasses.replace(
        empty_document(),
        revision_id=RevisionId(f"00000000-0000-4000-8000-{number:012d}"),
    )


def copied_document(payload: bytes, number: int = 2):
    object_id = ObjectId.from_bytes(payload)
    asset = AssetRef(
        digest=object_id,
        byte_count=len(payload),
        media_type=MediaType.PNG,
        width=1,
        height=1,
        profile_digest=ObjectId("1" * 64),
        import_policy=ImportPolicy.COPIED,
    )
    layer_id = LayerId("00000000-0000-4000-8000-000000000001")
    layer = PixelLayer(layer_id=layer_id, name="pixel", asset_digest=object_id)
    return (
        dataclasses.replace(
            revised(number),
            assets=(asset,),
            root_layer_ids=(layer_id,),
            layers=(layer,),
        ),
        object_id,
    )


class LayoutAndObjectTests(unittest.TestCase):
    def test_layout_has_all_seven_controlled_paths_and_refuses_extra_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project.kis"
            layout, generation = create_project(
                root,
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )
            layout.verify_structure()
            controlled_routes = (
                layout.head,
                layout.lock,
                layout.object_path(ObjectId("0" * 64)),
                layout.generation_path(generation.generation_id) / "manifest.json",
                layout.generation_path(generation.generation_id) / "objects.json",
                layout.autosave / "slot" / "HEAD",
                layout.metadata,
            )
            self.assertEqual(len(set(controlled_routes)), 7)
            (root / "unexpected").mkdir()
            with self.assertRaises(StoreError):
                layout.verify_structure()

    def test_symlink_object_is_refused_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project.kis"
            layout, _ = create_project(root, revised(1), limits=LIMITS, object_payloads={})
            payload = b"outside"
            record = ObjectRecord(ObjectId.from_bytes(payload), len(payload))
            outside = pathlib.Path(temporary) / "outside"
            outside.write_bytes(payload)
            shard = layout.objects / record.object_id.value[:2]
            shard.mkdir()
            (shard / record.object_id.value[2:]).symlink_to(outside)
            with self.assertRaises(StoreError):
                ObjectStore(layout, LIMITS).verify(record)

    def test_copied_object_and_generation_are_digest_bound_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = b"one-pixel-fixture"
            document, object_id = copied_document(payload)
            layout, generation = create_project(
                pathlib.Path(temporary) / "project.kis",
                document,
                limits=LIMITS,
                object_payloads={object_id: payload},
            )
            opened = open_project(layout, LIMITS)
            self.assertEqual(opened.generation, generation)
            self.assertEqual(opened.validated_classes, OPEN_VALIDATION_CLASSES)
            self.assertEqual(len(opened.validated_classes), 10)
            self.assertEqual(
                ObjectStore(layout, LIMITS).read(generation.objects[0]), payload
            )
            with self.assertRaises(SaveConflict):
                GenerationStore(layout, LIMITS).save(
                    dataclasses.replace(document, revision_id=RevisionId(
                        "00000000-0000-4000-8000-000000000003"
                    )),
                    object_payloads={},
                    expected_head=ObjectId("f" * 64),
                )

    def test_store_modules_import_domain_but_zero_render_or_engine_modules(self) -> None:
        store = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "store"
        )
        functional = tuple(
            sorted(
                path.name for path in store.glob("*.py") if path.name != "__init__.py"
            )
        )
        self.assertEqual(
            functional,
            (
                "gc.py",
                "generations.py",
                "layout.py",
                "locking.py",
                "objects.py",
                "recovery.py",
            ),
        )
        forbidden: list[str] = []
        for path in store.glob("*.py"):
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
                        ("kilix_image_shop.render", "kilix_image_shop.engine")
                    )
                )
        self.assertEqual(forbidden, [])

    def test_object_corruption_is_reported_by_the_named_hash_validation_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = b"one-pixel-fixture"
            document, object_id = copied_document(payload)
            layout, _ = create_project(
                pathlib.Path(temporary) / "project.kis",
                document,
                limits=LIMITS,
                object_payloads={object_id: payload},
            )
            layout.object_path(object_id).write_bytes(b"x" * len(payload))
            with self.assertRaises(OpenValidationError) as raised:
                open_project(layout, LIMITS)
            self.assertEqual(raised.exception.validation_class, "object-hashes")

    def test_missing_object_is_reported_by_the_named_presence_validation_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = b"one-pixel-fixture"
            document, object_id = copied_document(payload)
            layout, _ = create_project(
                pathlib.Path(temporary) / "project.kis",
                document,
                limits=LIMITS,
                object_payloads={object_id: payload},
            )
            layout.object_path(object_id).unlink()
            with self.assertRaises(OpenValidationError) as raised:
                open_project(layout, LIMITS)
            self.assertEqual(raised.exception.validation_class, "object-presence")

    def test_external_reference_drift_is_detected_and_gc_never_owns_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            payload = b"external-pixel"
            external = parent / "external.png"
            external.write_bytes(payload)
            object_id = ObjectId.from_bytes(payload)
            asset = AssetRef(
                digest=object_id,
                byte_count=len(payload),
                media_type=MediaType.PNG,
                width=1,
                height=1,
                profile_digest=ObjectId("1" * 64),
                import_policy=ImportPolicy.EXTERNAL_PORTABLE_RELATIVE,
                locator="external.png",
            )
            layer_id = LayerId("00000000-0000-4000-8000-000000000001")
            document = dataclasses.replace(
                revised(2),
                assets=(asset,),
                root_layer_ids=(layer_id,),
                layers=(
                    PixelLayer(
                        layer_id=layer_id,
                        name="external",
                        asset_digest=object_id,
                    ),
                ),
            )
            layout, _ = create_project(
                parent / "project.kis",
                document,
                limits=LIMITS,
                object_payloads={},
            )
            open_project(layout, LIMITS)
            result = apply_gc(preview_gc(layout, LIMITS), LIMITS)
            self.assertEqual(result.moved_objects, ())
            self.assertEqual(external.read_bytes(), payload)
            external.write_bytes(b"x" * len(payload))
            with self.assertRaises(OpenValidationError) as raised:
                open_project(layout, LIMITS)
            self.assertEqual(
                raised.exception.validation_class,
                "external-reference-drift",
            )


class SaveFaultInjectionTests(unittest.TestCase):
    def test_all_twelve_ordered_fault_points_preserve_atomic_head_semantics(self) -> None:
        outcomes: list[tuple[int, bool]] = []
        for target in range(1, SAVE_POINT_COUNT + 1):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                layout, original = create_project(
                    pathlib.Path(temporary) / "project.kis",
                    revised(1),
                    limits=LIMITS,
                    object_payloads={},
                )
                target_document = revised(2)

                def fail(point: int) -> None:
                    if point == target:
                        raise InjectedFailure(f"fault-{point}")

                with self.assertRaisesRegex(InjectedFailure, f"fault-{target}"):
                    GenerationStore(layout, LIMITS).save(
                        target_document,
                        object_payloads={},
                        expected_head=original.generation_id,
                        fault_hook=fail,
                    )
                opened = open_project(layout, LIMITS)
                committed = target >= 11
                self.assertEqual(
                    opened.generation.document,
                    target_document if committed else revised(1),
                )
                self.assertEqual(ProjectWriterLock(layout).held, False)
                with ProjectWriterLock(layout):
                    pass
                outcomes.append((target, committed))
        self.assertEqual(len(outcomes), 12)
        self.assertEqual(sum(committed for _, committed in outcomes), 2)

    def test_success_calls_each_of_twelve_points_once_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, original = create_project(
                pathlib.Path(temporary) / "project.kis",
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )
            observed: list[int] = []
            GenerationStore(layout, LIMITS).save(
                revised(2),
                object_payloads={},
                expected_head=original.generation_id,
                fault_hook=observed.append,
            )
            self.assertEqual(observed, list(range(1, 13)))


class RecoveryAndGCTests(unittest.TestCase):
    def test_open_never_silently_selects_a_valid_unreachable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, original = create_project(
                pathlib.Path(temporary) / "project.kis",
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )

            def orphan(point: int) -> None:
                if point == 10:
                    raise InjectedFailure("orphan-ready")

            with self.assertRaises(InjectedFailure):
                GenerationStore(layout, LIMITS).save(
                    revised(2),
                    object_payloads={},
                    expected_head=original.generation_id,
                    fault_hook=orphan,
                )
            layout.head.write_bytes(b"corrupt\n")
            before = layout.head.read_bytes()
            with self.assertRaises(OpenValidationError) as raised:
                open_project(layout, LIMITS)
            self.assertEqual(raised.exception.validation_class, "head-syntax")
            self.assertEqual(layout.head.read_bytes(), before)

    def test_recovery_is_named_previewed_and_retains_original_head_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, original = create_project(
                pathlib.Path(temporary) / "project.kis",
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )

            def fail_after_generation(point: int) -> None:
                if point == 10:
                    raise InjectedFailure("orphan-ready")

            with self.assertRaises(InjectedFailure):
                GenerationStore(layout, LIMITS).save(
                    revised(2),
                    object_payloads={},
                    expected_head=original.generation_id,
                    fault_hook=fail_after_generation,
                )
            candidates = list_recovery_candidates(layout)
            self.assertEqual(len(candidates), 2)
            orphan = next(item for item in candidates if item != original.generation_id)
            old_bytes = layout.head.read_bytes()
            preview = preview_recovery(layout, orphan, LIMITS)
            self.assertEqual(layout.head.read_bytes(), old_bytes)
            opened = apply_recovery(preview, LIMITS)
            self.assertEqual(opened.generation.document, revised(2))
            retained = (
                layout.autosave
                / f"recovery-{preview.original_head_sha256.value}"
                / "HEAD"
            )
            self.assertEqual(retained.read_bytes(), old_bytes)

    def test_gc_previews_revalidates_quarantines_and_retires_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, _ = create_project(
                pathlib.Path(temporary) / "project.kis",
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )
            payload = b"unreachable-object"
            record = ObjectRecord(ObjectId.from_bytes(payload), len(payload))
            store = ObjectStore(layout, LIMITS)
            with ProjectWriterLock(layout):
                staged = store.stage_missing((record,), {record.object_id: payload})
                store.publish(staged)
            preview = preview_gc(layout, LIMITS)
            self.assertEqual(preview.unreachable_objects, (record,))
            result = apply_gc(preview, LIMITS)
            self.assertEqual(result.moved_objects, (record,))
            self.assertFalse(layout.object_path(record.object_id).exists())
            self.assertIsNotNone(result.quarantine)
            assert result.quarantine is not None
            self.assertTrue(result.quarantine.is_dir())
            open_project(layout, LIMITS)
            retire_quarantine(layout, result.quarantine)
            self.assertFalse(result.quarantine.exists())

    def test_gc_refuses_when_an_autosave_root_changes_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout, generation = create_project(
                pathlib.Path(temporary) / "project.kis",
                revised(1),
                limits=LIMITS,
                object_payloads={},
            )
            preview = preview_gc(layout, LIMITS)
            slot = layout.autosave / "changed"
            slot.mkdir()
            (slot / "HEAD").write_bytes((generation.generation_id.value + "\n").encode())
            with self.assertRaises(StoreError):
                apply_gc(preview, LIMITS)


if __name__ == "__main__":
    unittest.main()
