from __future__ import annotations

import ast
import dataclasses
import fcntl
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId
from kilix_image_shop.domain.layers import PixelLayer
from kilix_image_shop.engine.api import (
    CancelToken,
    CancelledOrStaleWork,
    FakeImageEngine,
    PixelFormat,
    PixelSpec,
)
from kilix_image_shop.export.pipeline import (
    CodecInspection,
    ExportLimits,
    ExportPipelineError,
    ExportPublicationIndeterminate,
    export_document,
)
from kilix_image_shop.export.presets import (
    AlphaPolicy,
    DETERMINISM_BINDINGS,
    ExportFormat,
    ExportPreset,
    ExportPresetError,
    MAX_PRESET_BYTES,
    MetadataPolicy,
    deterministic_preset,
    document_object_ids,
)
from kilix_image_shop.export.provenance import (
    ExportArtifact,
    ExportProvenance,
    ExportProvenanceError,
    project_export_provenance,
)
from kilix_image_shop.render.plan import derive_render_plan

from domain_fixtures import empty_document, object_id, sample_document


GENERATION = ObjectId.from_bytes(b"synthetic-export-generation")
LIMITS = ExportLimits(
    max_raw_bytes=4_194_304,
    max_encoded_bytes=1_048_576,
    max_sidecar_bytes=1_048_576,
    max_tiles=32,
)


def render_document():
    profile_payload = b"synthetic-export-icc-profile"
    profile = ObjectId.from_bytes(profile_payload)
    pixel_payload = bytes(range(32)) * 4
    pixel_digest = ObjectId.from_bytes(pixel_payload)
    base = empty_document()
    compatibility = dataclasses.replace(
        base.engine_compatibility,
        working_profile=profile,
    )
    asset = AssetRef(
        digest=pixel_digest,
        byte_count=len(pixel_payload),
        media_type=MediaType.PNG,
        width=4,
        height=4,
        profile_digest=profile,
        import_policy=ImportPolicy.COPIED,
    )
    identity = LayerId("00000000-0000-4000-8000-000000000001")
    layer = PixelLayer(
        layer_id=identity,
        name="pixels",
        asset_digest=pixel_digest,
    )
    document = dataclasses.replace(
        base,
        canvas=dataclasses.replace(base.canvas, width=4, height=4),
        colour=dataclasses.replace(base.colour, working_profile=profile),
        engine_compatibility=compatibility,
        assets=(asset,),
        root_layer_ids=(identity,),
        layers=(layer,),
    )
    return document, profile_payload, pixel_payload


class CountingEngine(FakeImageEngine):
    def __init__(self, document) -> None:
        super().__init__(compatibility_digest=document.engine_compatibility.digest)
        self.export_calls: list[tuple[object, ...]] = []

    def export_tiles(self, requests, *, cancel):
        self.export_calls.append(requests)
        return super().export_tiles(requests, cancel=cancel)


def started_engine(document, profile_payload: bytes, pixel_payload: bytes):
    engine = CountingEngine(document)
    engine.start()
    cancel = CancelToken()
    engine.register_profile(
        profile_payload,
        document.colour.working_profile,
        cancel=cancel,
    )
    engine.import_pixels(
        pixel_payload,
        extent=Rect(0, 0, 4, 4),
        spec=PixelSpec.colour(
            PixelFormat.RGBA_U16,
            document.colour.working_profile,
            alpha_association=document.engine_compatibility.alpha_association,
        ),
        revision=document.revision_id,
        cancel=cancel,
    )
    return engine


class FakeCodecWorker:
    isolated = True

    def __init__(
        self,
        *,
        failure: bool = False,
        cancel_during_encode: bool = False,
        inspection_updates: dict[str, object] | None = None,
        metadata_keys: tuple[str, ...] = (),
    ) -> None:
        self.failure = failure
        self.cancel_during_encode = cancel_during_encode
        self.inspection_updates = inspection_updates or {}
        self.metadata_keys = metadata_keys
        self.raw_sizes: list[int] = []
        self.raw_access_modes: list[int] = []

    def encode(self, raw_fd, output_fd, *, raw_spec, preset, cancel) -> None:
        cancel.raise_if_cancelled()
        if self.failure:
            raise ExportPipelineError("synthetic codec failure")
        raw_size = os.fstat(raw_fd).st_size
        self.raw_sizes.append(raw_size)
        self.raw_access_modes.append(fcntl.fcntl(raw_fd, fcntl.F_GETFL) & os.O_ACCMODE)
        raw = os.pread(raw_fd, raw_size, 0)
        payload = (
            b"kilix-synthetic-codec/v1\0"
            + preset.digest.value.encode("ascii")
            + ObjectId.from_bytes(raw).value.encode("ascii")
        )
        os.ftruncate(output_fd, 0)
        os.pwrite(output_fd, payload, 0)
        if self.cancel_during_encode:
            cancel.cancel()

    def inspect(self, output_fd, *, preset, cancel) -> CodecInspection:
        cancel.raise_if_cancelled()
        values: dict[str, object] = {
            "export_format": preset.export_format,
            "width": preset.width,
            "height": preset.height,
            "profile_digest": preset.output_profile,
            "metadata_keys": self.metadata_keys,
        }
        values.update(self.inspection_updates)
        return CodecInspection(**values)


class ExportPresetTests(unittest.TestCase):
    def test_four_formats_bind_all_ten_deterministic_inputs_and_round_trip(self) -> None:
        document, _, _ = render_document()
        presets = tuple(
            deterministic_preset(document, GENERATION, export_format)
            for export_format in ExportFormat
        )
        self.assertEqual(len(presets), 4)
        self.assertEqual(len({item.digest for item in presets}), 4)
        for preset in presets:
            self.assertEqual(preset.binding_groups, DETERMINISM_BINDINGS)
            self.assertEqual(len(preset.binding_groups), 10)
            self.assertEqual(preset.metadata_policy, MetadataPolicy.STRIP)
            self.assertEqual(
                ExportPreset.from_bytes(preset.canonical_bytes()),
                preset,
            )
        jpeg = next(item for item in presets if item.export_format is ExportFormat.JPEG)
        self.assertEqual(jpeg.alpha_policy, AlphaPolicy.OPAQUE_BACKGROUND)
        self.assertTrue(
            all(
                item.alpha_policy is AlphaPolicy.PRESERVE
                for item in presets
                if item.export_format is not ExportFormat.JPEG
            )
        )

    def test_preset_refuses_ambient_values_and_jpeg_alpha(self) -> None:
        document, _, _ = render_document()
        preset = deterministic_preset(document, GENERATION, ExportFormat.PNG)
        with self.assertRaises(ExportPresetError):
            dataclasses.replace(preset, locale="ambient")
        with self.assertRaises(ExportPresetError):
            dataclasses.replace(
                deterministic_preset(document, GENERATION, ExportFormat.JPEG),
                alpha_policy=AlphaPolicy.PRESERVE,
                background_rgb_u16=None,
            )
        value = json.loads(preset.canonical_bytes())
        value["determinismBindings"].pop()
        hostile = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        with self.assertRaises(ExportPresetError):
            ExportPreset.from_bytes(hostile)
        with self.assertRaises(ExportPresetError):
            ExportPreset.from_bytes(b" " * (MAX_PRESET_BYTES + 1))
        with self.assertRaises(ExportPresetError):
            dataclasses.replace(
                preset,
                working_format=PixelFormat.RGBA_U16.value,
            )

    def test_object_closure_is_sorted_unique_and_covers_visible_content(self) -> None:
        document = sample_document()
        closure = document_object_ids(document)
        self.assertEqual(closure, tuple(sorted(set(closure), key=lambda item: item.value)))
        expected = {
            object_id("1"),
            object_id("6"),
            object_id("7"),
            object_id("8"),
            object_id("9"),
            object_id("a"),
            object_id("b"),
        }
        self.assertTrue(expected.issubset(set(closure)))


class ProvenanceTests(unittest.TestCase):
    def test_sidecar_round_trip_redacts_prompts_by_default_and_can_include_them(self) -> None:
        document = sample_document()
        original = document.provenance[0]
        operation = dataclasses.replace(original, prompt="private prompt")
        layers = tuple(
            dataclasses.replace(layer, operation_provenance=operation)
            if getattr(layer, "operation_provenance", None) == original
            else layer
            for layer in document.layers
        )
        document = dataclasses.replace(
            document,
            layers=layers,
            provenance=(operation,),
        )
        plan = derive_render_plan(document)
        preset = deterministic_preset(document, GENERATION, ExportFormat.PNG)
        artifact = ExportArtifact(
            ObjectId.from_bytes(b"encoded"),
            7,
            ExportFormat.PNG,
            preset.width,
            preset.height,
            preset.output_profile,
            (),
        )
        redacted = project_export_provenance(document, plan, preset, artifact)
        self.assertNotIn(b"private prompt", redacted.canonical_bytes())
        self.assertEqual(len(redacted.operations), 1)
        self.assertFalse(redacted.operations[0].prompt_included)
        self.assertEqual(
            ExportProvenance.from_bytes(
                redacted.canonical_bytes(),
                maximum_bytes=1_048_576,
            ),
            redacted,
        )
        included = project_export_provenance(
            document,
            plan,
            preset,
            artifact,
            include_prompts=True,
        )
        self.assertIn(b"private prompt", included.canonical_bytes())
        with self.assertRaises(ExportProvenanceError):
            project_export_provenance(
                document,
                plan,
                dataclasses.replace(
                    preset,
                    object_closure_digest=object_id("f"),
                ),
                artifact,
            )

    def test_sidecar_refuses_duplicate_unknown_and_noncanonical_json(self) -> None:
        with self.assertRaises(ExportProvenanceError):
            ExportProvenance.from_bytes(
                b'{"schema":"x","schema":"y"}\n',
                maximum_bytes=1024,
            )
        with self.assertRaises(ExportProvenanceError):
            ExportProvenance.from_bytes(b"{}", maximum_bytes=1024)


class ExportPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document, profile, pixels = render_document()
        self.engine = started_engine(self.document, profile, pixels)
        self.plan = derive_render_plan(self.document)

    def tearDown(self) -> None:
        self.engine.close()

    def test_all_four_formats_publish_verified_image_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for export_format in ExportFormat:
                with self.subTest(export_format=export_format):
                    preset = deterministic_preset(
                        self.document,
                        GENERATION,
                        export_format,
                    )
                    destination = root / f"image.{export_format.value}"
                    worker = FakeCodecWorker()
                    result = export_document(
                        self.document,
                        self.plan,
                        preset,
                        destination,
                        engine=self.engine,
                        worker=worker,
                        limits=LIMITS,
                        cancel=CancelToken(),
                    )
                    self.assertEqual(
                        ObjectId.from_bytes(destination.read_bytes()),
                        result.artifact.image_digest,
                    )
                    retained = ExportProvenance.from_bytes(
                        result.sidecar.read_bytes(),
                        maximum_bytes=LIMITS.max_sidecar_bytes,
                    )
                    self.assertEqual(retained, result.provenance)
                    self.assertEqual(result.tile_count, 1)
                    self.assertEqual(worker.raw_sizes, [4 * 4 * 8])
                    self.assertEqual(worker.raw_access_modes, [os.O_RDONLY])

    def test_large_output_streams_two_individually_bounded_tiles(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
            width=1921,
            height=2,
        )
        with tempfile.TemporaryDirectory() as temporary:
            worker = FakeCodecWorker()
            result = export_document(
                self.document,
                self.plan,
                preset,
                pathlib.Path(temporary) / "large.png",
                engine=self.engine,
                worker=worker,
                limits=LIMITS,
                cancel=CancelToken(),
            )
        recent = self.engine.export_calls[-2:]
        self.assertEqual(result.tile_count, 2)
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(len(batch) == 1 for batch in recent))
        self.assertTrue(
            all(
                batch[0].destination.width <= 1920
                and batch[0].destination.height <= 1080
                for batch in recent
            )
        )
        self.assertEqual(worker.raw_sizes, [1921 * 2 * 8])

    def test_codec_failure_and_cancellation_change_zero_existing_targets(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for name, worker, cancel in (
                ("failure", FakeCodecWorker(failure=True), CancelToken()),
                (
                    "cancel",
                    FakeCodecWorker(cancel_during_encode=True),
                    CancelToken(),
                ),
            ):
                with self.subTest(name=name):
                    destination = root / f"{name}.png"
                    sidecar = destination.with_name(destination.name + ".provenance.json")
                    destination.write_bytes(b"old image")
                    sidecar.write_bytes(b"old sidecar")
                    expected_exception = (
                        ExportPipelineError
                        if name == "failure"
                        else CancelledOrStaleWork
                    )
                    with self.assertRaises(expected_exception):
                        export_document(
                            self.document,
                            self.plan,
                            preset,
                            destination,
                            engine=self.engine,
                            worker=worker,
                            limits=LIMITS,
                            cancel=cancel,
                        )
                    self.assertEqual(destination.read_bytes(), b"old image")
                    self.assertEqual(sidecar.read_bytes(), b"old sidecar")
                    self.assertEqual(tuple(root.glob(".kilix-export-*")), ())

    def test_image_replace_failure_restores_both_existing_targets(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            destination = root / "replace.png"
            sidecar = destination.with_name(destination.name + ".provenance.json")
            destination.write_bytes(b"old image")
            sidecar.write_bytes(b"old sidecar")
            real_replace = os.replace

            def fail_image_replace(source, target, *args, **kwargs):
                if str(source).endswith(".image") and target == destination.name:
                    raise OSError("synthetic image replace failure")
                return real_replace(source, target, *args, **kwargs)

            with mock.patch(
                "kilix_image_shop.export.pipeline.os.replace",
                side_effect=fail_image_replace,
            ):
                with self.assertRaises(ExportPipelineError):
                    export_document(
                        self.document,
                        self.plan,
                        preset,
                        destination,
                        engine=self.engine,
                        worker=FakeCodecWorker(),
                        limits=LIMITS,
                        cancel=CancelToken(),
                    )
            self.assertEqual(destination.read_bytes(), b"old image")
            self.assertEqual(sidecar.read_bytes(), b"old sidecar")
            self.assertEqual(tuple(root.glob(".kilix-export-*")), ())

    def test_committed_backup_retirement_failure_is_explicitly_indeterminate(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            destination = root / "indeterminate.png"
            sidecar = destination.with_name(destination.name + ".provenance.json")
            destination.write_bytes(b"old image")
            sidecar.write_bytes(b"old sidecar")
            real_unlink = os.unlink

            def fail_backup_unlink(path, *args, **kwargs):
                if str(path).endswith(".backup"):
                    raise OSError("synthetic backup retirement failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch(
                "kilix_image_shop.export.pipeline.os.unlink",
                side_effect=fail_backup_unlink,
            ):
                with self.assertRaises(ExportPublicationIndeterminate):
                    export_document(
                        self.document,
                        self.plan,
                        preset,
                        destination,
                        engine=self.engine,
                        worker=FakeCodecWorker(),
                        limits=LIMITS,
                        cancel=CancelToken(),
                    )
            self.assertNotEqual(destination.read_bytes(), b"old image")
            retained = ExportProvenance.from_bytes(
                sidecar.read_bytes(),
                maximum_bytes=LIMITS.max_sidecar_bytes,
            )
            self.assertEqual(retained.artifact.image_digest, ObjectId.from_bytes(
                destination.read_bytes()
            ))

    def test_preset_closure_and_plugin_drift_fail_before_engine_work(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        cases = (
            dataclasses.replace(preset, object_closure_digest=object_id("f")),
            dataclasses.replace(preset, plugin_tree_digest=object_id("f")),
        )
        before_calls = len(self.engine.export_calls)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for index, hostile in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(
                    ExportPipelineError
                ):
                    export_document(
                        self.document,
                        self.plan,
                        hostile,
                        root / f"hostile-{index}.png",
                        engine=self.engine,
                        worker=FakeCodecWorker(),
                        limits=LIMITS,
                        cancel=CancelToken(),
                    )
        self.assertEqual(len(self.engine.export_calls), before_calls)

    def test_format_profile_geometry_and_metadata_mismatches_publish_zero_files(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        cases = (
            {"export_format": ExportFormat.TIFF},
            {"width": 3},
            {"height": 3},
            {"profile_digest": object_id("f")},
            {"metadata_keys": ("exif.gps",)},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for index, updates in enumerate(cases):
                with self.subTest(updates=updates):
                    metadata = updates.pop("metadata_keys", ())
                    worker = FakeCodecWorker(
                        inspection_updates=updates,
                        metadata_keys=metadata,
                    )
                    destination = root / f"refused-{index}.png"
                    with self.assertRaises(ExportPipelineError):
                        export_document(
                            self.document,
                            self.plan,
                            preset,
                            destination,
                            engine=self.engine,
                            worker=worker,
                            limits=LIMITS,
                            cancel=CancelToken(),
                        )
                    self.assertFalse(destination.exists())
                    self.assertFalse(
                        destination.with_name(
                            destination.name + ".provenance.json"
                        ).exists()
                    )

    def test_symlink_destination_is_refused_without_touching_its_target(self) -> None:
        preset = deterministic_preset(
            self.document,
            GENERATION,
            ExportFormat.PNG,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            target = root / "target"
            target.write_bytes(b"untouched")
            destination = root / "link.png"
            destination.symlink_to(target)
            with self.assertRaises(ExportPipelineError):
                export_document(
                    self.document,
                    self.plan,
                    preset,
                    destination,
                    engine=self.engine,
                    worker=FakeCodecWorker(),
                    limits=LIMITS,
                    cancel=CancelToken(),
                )
            self.assertEqual(target.read_bytes(), b"untouched")


class ExportDependencyTests(unittest.TestCase):
    def test_three_export_modules_are_provider_and_native_runtime_free(self) -> None:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "export"
        )
        modules = tuple(
            sorted(path.name for path in root.glob("*.py") if path.name != "__init__.py")
        )
        self.assertEqual(modules, ("pipeline.py", "presets.py", "provenance.py"))
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
                    if name.startswith(("gi", "kilix_image_shop.ops"))
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
