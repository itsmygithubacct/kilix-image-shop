from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest

from kilix_image_shop.application import (
    ApplicationError,
    ApplicationPostCommitError,
    ApplicationService,
)
from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.commands import (
    ApplyOperationOutput,
    ImportAsset,
    ResolvedObject,
    SetLayerProperty,
)
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import OperationProvenance, Parameter, PixelLayer
from kilix_image_shop.history.budget import HistoryBudget
from kilix_image_shop.history.spill import SpillStore
from kilix_image_shop.history.stack import HistoryError, HistoryStack
from kilix_image_shop.ports import ObjectPayload

from domain_fixtures import empty_document, layer_id, object_id, sample_document


def revision(number: int) -> RevisionId:
    return RevisionId(f"00000000-0000-4000-8000-{number:012d}")


def existing_references(state) -> dict[ObjectId, ResolvedObject]:
    sizes = {
        object_id("1"): 1,
        object_id("6"): 128,
        object_id("7"): 96,
        object_id("9"): 64 * 48,
        object_id("a"): 1,
        object_id("b"): 1,
    }
    return {
        identity: ResolvedObject(identity, byte_count)
        for identity, byte_count in sizes.items()
        if identity == state.colour.working_profile
        or identity in {
            asset.digest for asset in state.assets
        }
        or identity in {
            asset.profile_digest for asset in state.assets
        }
        or identity
        in {
            getattr(getattr(layer, "mask", None), "object_id", None)
            for layer in state.layers
        }
        or identity
        in {
            getattr(layer, "font_digest", None) for layer in state.layers
        }
        or identity
        in {
            getattr(layer, "preview_asset_digest", None) for layer in state.layers
        }
        or identity == getattr(state.selection, "object_id", None)
    }


class MemoryObjects:
    def __init__(self, references) -> None:
        self.references = dict(references)
        self.writes: list[ObjectPayload] = []
        self.fail_write = False
        self.invalid: set[ObjectId] = set()

    def write(self, value: ObjectPayload) -> None:
        if self.fail_write:
            raise RuntimeError("synthetic write failure")
        self.writes.append(value)
        self.references[value.reference.object_id] = value.reference

    def verify(self, reference: ResolvedObject) -> bool:
        return (
            reference.object_id not in self.invalid
            and self.references.get(reference.object_id) == reference
        )

    def resolve(self, object_id: ObjectId) -> ResolvedObject:
        return self.references[object_id]


class MemoryEffects:
    def __init__(self) -> None:
        self.published = []
        self.restored = []
        self.fail = False
        self.callback = None
        self.reentry_error = None

    def publish(self, before, result, effects) -> None:
        if self.fail:
            raise RuntimeError("synthetic post-commit failure")
        if self.callback is not None:
            try:
                self.callback()
            except Exception as exc:
                self.reentry_error = exc
        self.published.append((before, result, effects))

    def restore(self, state, invalidated_layer_ids) -> None:
        if self.fail:
            raise RuntimeError("synthetic post-commit failure")
        self.restored.append((state, invalidated_layer_ids))


class MemoryPresentation:
    def __init__(self) -> None:
        self.values = []

    def document_changed(self, state, *, can_undo, can_redo) -> None:
        self.values.append((state, can_undo, can_redo))


class MemoryProject:
    def __init__(self) -> None:
        self.states = []

    def save(self, state):
        self.states.append(state)
        return ObjectId.from_bytes(state.canonical_bytes())


class ApplicationTests(unittest.TestCase):
    def make_service(self, temporary: str, state=None):
        document = sample_document() if state is None else state
        spill = SpillStore.create(
            pathlib.Path(temporary),
            document.document_id,
            max_record_bytes=2_000_000,
        )
        history = HistoryStack(
            document,
            HistoryBudget(16, 2_000_000, 2_000_000),
            spill,
        )
        objects = MemoryObjects(existing_references(document))
        effects = MemoryEffects()
        presentation = MemoryPresentation()
        service = ApplicationService(
            history,
            objects,
            effects,
            presentation=presentation,
        )
        return service, history, objects, effects, presentation

    def test_command_publishes_one_revision_history_entry_and_two_render_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, history, _, effects, presentation = self.make_service(temporary)
            command = SetLayerProperty(
                expected_revision=service.state.revision_id,
                new_revision=revision(10),
                layer_id=layer_id(1),
                name="renamed",
            )
            result = service.execute(command)
            self.assertEqual(service.state.revision_id, revision(10))
            self.assertEqual(result.state.layer_map[layer_id(1)].name, "renamed")
            self.assertEqual(result.history_usage.entries, 1)
            self.assertEqual(len(effects.published), 1)
            self.assertEqual(len(effects.published[0][2]), 2)
            self.assertEqual(len(presentation.values), 1)
            self.assertTrue(history.can_undo)

    def test_required_object_write_precedes_publication_and_is_digest_verified(self) -> None:
        document = empty_document()
        payload = bytes(range(16))
        identity = ObjectId.from_bytes(payload)
        asset = AssetRef(
            digest=identity,
            byte_count=len(payload),
            media_type=MediaType.PNG,
            width=2,
            height=1,
            profile_digest=document.colour.working_profile,
            import_policy=ImportPolicy.COPIED,
        )
        layer = PixelLayer(
            layer_id=LayerId("00000000-0000-4000-8000-000000000001"),
            name="imported",
            asset_digest=identity,
        )
        command = ImportAsset(
            expected_revision=document.revision_id,
            new_revision=revision(10),
            asset=asset,
            layer=layer,
            parent_id=None,
            index=0,
        )
        value = ObjectPayload(ResolvedObject(identity, len(payload)), payload)
        with tempfile.TemporaryDirectory() as temporary:
            service, history, objects, effects, _ = self.make_service(
                temporary,
                document,
            )
            result = service.execute(command, payloads=(value,))
            self.assertEqual(objects.writes, [value])
            self.assertEqual(result.state.assets, (asset,))
            self.assertEqual(history.usage.entries, 1)
            self.assertEqual(len(effects.published), 1)

    def test_missing_unexpected_or_failed_object_write_changes_zero_document_states(self) -> None:
        document = empty_document()
        payload = b"new immutable pixels"
        identity = ObjectId.from_bytes(payload)
        asset = AssetRef(
            digest=identity,
            byte_count=len(payload),
            media_type=MediaType.PNG,
            width=1,
            height=1,
            profile_digest=document.colour.working_profile,
            import_policy=ImportPolicy.COPIED,
        )
        layer = PixelLayer(
            layer_id=LayerId("00000000-0000-4000-8000-000000000001"),
            name="imported",
            asset_digest=identity,
        )
        command = ImportAsset(
            expected_revision=document.revision_id,
            new_revision=revision(10),
            asset=asset,
            layer=layer,
            parent_id=None,
            index=0,
        )
        value = ObjectPayload(ResolvedObject(identity, len(payload)), payload)
        for mode in ("missing", "failed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                service, history, objects, effects, _ = self.make_service(
                    temporary,
                    document,
                )
                before = service.state
                if mode == "failed":
                    objects.fail_write = True
                with self.assertRaises(Exception):
                    service.execute(
                        command,
                        payloads=() if mode == "missing" else (value,),
                    )
                self.assertIs(service.state, before)
                self.assertEqual(history.usage.entries, 0)
                self.assertEqual(effects.published, [])

        with tempfile.TemporaryDirectory() as temporary:
            service, history, _, effects, _ = self.make_service(temporary)
            unrelated_payload = b"unrelated"
            unrelated = ObjectPayload(
                ResolvedObject(
                    ObjectId.from_bytes(unrelated_payload),
                    len(unrelated_payload),
                ),
                unrelated_payload,
            )
            with self.assertRaises(ApplicationError):
                service.execute(
                    SetLayerProperty(
                        expected_revision=service.state.revision_id,
                        new_revision=revision(11),
                        layer_id=layer_id(1),
                        name="valid command",
                    ),
                    payloads=(unrelated,),
                )
            self.assertEqual(history.usage.entries, 0)
            self.assertEqual(effects.published, [])

    def test_undo_redo_revalidate_objects_and_publish_distinct_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, _, objects, effects, _ = self.make_service(temporary)
            service.execute(
                SetLayerProperty(
                    expected_revision=service.state.revision_id,
                    new_revision=revision(10),
                    layer_id=layer_id(1),
                    name="renamed",
                )
            )
            undo = service.undo(
                expected_revision=revision(10),
                new_revision=revision(11),
            )
            self.assertEqual(undo.transition.state.layer_map[layer_id(1)].name, "Pixels")
            redo = service.redo(
                expected_revision=revision(11),
                new_revision=revision(12),
            )
            self.assertEqual(redo.transition.state.layer_map[layer_id(1)].name, "renamed")
            self.assertEqual(len(effects.restored), 2)
            objects.invalid.add(object_id("6"))
            before = service.state
            with self.assertRaises(HistoryError):
                service.undo(
                    expected_revision=revision(12),
                    new_revision=revision(13),
                )
            self.assertIs(service.state, before)

    def test_post_commit_adapter_failure_reports_committed_state_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, history, _, effects, _ = self.make_service(temporary)
            effects.fail = True
            with self.assertRaises(ApplicationPostCommitError) as captured:
                service.execute(
                    SetLayerProperty(
                        expected_revision=service.state.revision_id,
                        new_revision=revision(10),
                        layer_id=layer_id(1),
                        name="committed",
                    )
                )
            self.assertEqual(service.state.revision_id, revision(10))
            self.assertEqual(captured.exception.result.state, service.state)
            self.assertEqual(history.usage.entries, 1)

    def test_post_commit_callback_cannot_reenter_application_use_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, history, _, effects, _ = self.make_service(temporary)
            effects.callback = lambda: service.execute(
                SetLayerProperty(
                    expected_revision=service.state.revision_id,
                    new_revision=revision(11),
                    layer_id=layer_id(1),
                    name="reentrant",
                )
            )
            service.execute(
                SetLayerProperty(
                    expected_revision=service.state.revision_id,
                    new_revision=revision(10),
                    layer_id=layer_id(1),
                    name="outer",
                )
            )
            self.assertIsInstance(effects.reentry_error, ApplicationError)
            self.assertEqual(history.usage.entries, 1)
            self.assertEqual(service.state.layer_map[layer_id(1)].name, "outer")

    def test_apply_operation_output_enters_through_the_same_command_transaction(self) -> None:
        document = empty_document()
        payload = bytes(range(16))
        identity = ObjectId.from_bytes(payload)
        provenance = OperationProvenance(
            schema=OperationProvenance.SCHEMA,
            operation="kilix.generate",
            provider="kilix.fake-provider",
            model_digest=None,
            runtime_digest=object_id("8"),
            prompt=None,
            seed=None,
            parameters=(Parameter("fixture-only", True),),
            source_layer_digest=None,
            occurred_at="2026-08-30T00:00:00+00:00",
        )
        asset = AssetRef(
            digest=identity,
            byte_count=len(payload),
            media_type=MediaType.PNG,
            width=2,
            height=1,
            profile_digest=document.colour.working_profile,
            import_policy=ImportPolicy.COPIED,
        )
        layer = PixelLayer(
            layer_id=LayerId("00000000-0000-4000-8000-000000000009"),
            name="generated",
            asset_digest=identity,
            operation_provenance=provenance,
        )
        command = ApplyOperationOutput(
            expected_revision=document.revision_id,
            new_revision=revision(10),
            provenance=provenance,
            output_asset=asset,
            output_layer=layer,
        )
        value = ObjectPayload(ResolvedObject(identity, len(payload)), payload)
        with tempfile.TemporaryDirectory() as temporary:
            service, _, _, _, _ = self.make_service(temporary, document)
            result = service.execute(command, payloads=(value,))
            self.assertEqual(result.state.root_layer_ids, (layer.layer_id,))
            self.assertEqual(result.state.provenance, (provenance,))

    def test_save_captures_exact_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service, _, _, _, _ = self.make_service(temporary)
            project = MemoryProject()
            generation = service.save(project)
            self.assertEqual(project.states, [service.state])
            self.assertEqual(generation, ObjectId.from_bytes(service.state.canonical_bytes()))


class ApplicationDependencyTests(unittest.TestCase):
    def test_frozen_functional_module_population_is_exactly_forty(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kilix_image_shop"
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if path.name != "__init__.py"
        }
        expected = {
            "application.py",
            "ports.py",
            "domain/identifiers.py",
            "domain/geometry.py",
            "domain/color.py",
            "domain/assets.py",
            "domain/layers.py",
            "domain/document.py",
            "domain/commands.py",
            "engine/api.py",
            "engine/compatibility.py",
            "engine/formats.py",
            "engine/runtime.py",
            "render/plan.py",
            "render/graph.py",
            "render/proxy.py",
            "render/scheduler.py",
            "render/compositor.py",
            "store/layout.py",
            "store/objects.py",
            "store/generations.py",
            "store/locking.py",
            "store/recovery.py",
            "store/gc.py",
            "history/budget.py",
            "history/stack.py",
            "history/spill.py",
            "editing/adjustments.py",
            "editing/selection.py",
            "editing/masking.py",
            "editing/transforms.py",
            "editing/paint.py",
            "editing/text.py",
            "export/presets.py",
            "export/pipeline.py",
            "export/provenance.py",
            "ops/messages.py",
            "ops/state.py",
            "ops/orchestrator.py",
            "ops/diagnostics.py",
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 40)

    def test_application_and_ports_import_zero_adapter_families(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kilix_image_shop"
        forbidden: list[str] = []
        for name in ("application.py", "ports.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                forbidden.extend(
                    value
                    for value in names
                    if value.startswith(
                        (
                            "gi",
                            "os",
                            "pathlib",
                            "kilix_image_shop.engine.runtime",
                            "kilix_image_shop.ops.background_removal",
                        )
                    )
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
