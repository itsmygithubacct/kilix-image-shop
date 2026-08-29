from __future__ import annotations

import dataclasses
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from engine_registry_fixtures import synthetic_registry
from test_engine_api import (
    PROFILE_A,
    PROFILE_A_BYTES,
    PROFILE_B,
    PROFILE_B_BYTES,
    REVISION,
    TEXT_RASTER_PAYLOAD,
    colour_spec,
    full_graph,
)
from kilix_image_shop.domain.color import (
    AlphaAssociation,
    ConversionPolicy,
    EngineCompatibility,
)
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.domain.layers import Adjustment, AdjustmentId, Parameter
from kilix_image_shop.engine.api import (
    AdjustmentParameters,
    CancelToken,
    CancelledOrStaleWork,
    DecodeRefusal,
    GraphNodeKind,
    ImageEngine,
    InvalidGraph,
    MaskTileUpdate,
    PixelFormat,
    PixelSpec,
    TileRequest,
    UnsupportedOperation,
)
from kilix_image_shop.engine.compatibility import (
    BABL_NATIVE_VERSION,
    BABL_PACKAGE_VERSION,
    EXPECTED_OPERATION_COUNT,
    GEGL_NATIVE_VERSION,
    GEGL_PACKAGE_VERSION,
    GI_ORIGIN,
    H0_TILE_CACHE_BYTES,
    NativeObservation,
    PACKAGE_GROUP_ID,
    PYTHON_GI_PACKAGE_VERSION,
    RuntimeConfiguration,
)
from kilix_image_shop.engine.runtime import (
    BufferBinding,
    CompiledGraphPlan,
    ImageRuntime,
    Od7ImageEngine,
    ProfileBinding,
    RuntimeProcessGuard,
)
from kilix_image_shop.render.proxy import ProxyBuildPlan, ProxyCache


GROUP_BYTES = b'{"group":"plebian.f115.image-engine","synthetic":true}\n'
PLUGIN_BYTES = b'{"schema":"synthetic-plugin-tree/v1"}\n'
GI_DIGEST = ObjectId("3" * 64)


class CompilerBackend:
    def __init__(self, registry) -> None:
        native = set(registry.native_operations)
        native.update(
            f"gegl:synthetic-{index:03d}"
            for index in range(EXPECTED_OPERATION_COUNT - len(native))
        )
        self.identity = NativeObservation(
            gegl_native_version=GEGL_NATIVE_VERSION,
            babl_native_version=BABL_NATIVE_VERSION,
            gegl_package_version=GEGL_PACKAGE_VERSION,
            babl_package_version=BABL_PACKAGE_VERSION,
            python_gi_package_version=PYTHON_GI_PACKAGE_VERSION,
            gi_origin=GI_ORIGIN,
            gi_file_digest=GI_DIGEST,
            operations=tuple(sorted(native)),
        )
        self.values: dict[str, object] = {}
        self.profile_paths: list[pathlib.Path] = []
        self.imports: list[tuple[bytes, Rect, str, object]] = []
        self.plans: list[CompiledGraphPlan] = []
        self.plan_buffers: list[dict[str, object]] = []
        self.plan_profiles: list[dict[ObjectId, pathlib.Path]] = []
        self.released_buffers: list[object] = []
        self.released_graphs: list[object] = []
        self.lifecycle: list[str] = []
        self.proxy_builds: list[tuple[int, Rect, str, object, object]] = []
        self.proxy_reads: list[tuple[object, Rect, str]] = []
        self.tile_renders: list[tuple[object, Rect, Rect, str]] = []
        self.mask_duplicates: list[tuple[object, object]] = []
        self.mask_writes: list[tuple[object, Rect, str, bytes]] = []
        self.cancel_on_import: CancelToken | None = None
        self.cancel_on_compile: CancelToken | None = None
        self.cancel_on_proxy_build: CancelToken | None = None
        self.cancel_on_render: CancelToken | None = None
        self.cancel_on_mask_write: CancelToken | None = None
        self.shutdown_count = 0

    def initialize(self) -> None:
        pass

    def configure(self, values: tuple[tuple[str, object], ...]) -> None:
        self.values = dict(values)

    def read_configuration(self, names: tuple[str, ...]) -> dict[str, object]:
        return {name: self.values[name] for name in names}

    def observe(self) -> NativeObservation:
        return self.identity

    def verify_operation_registry(self, registry) -> None:
        if not set(registry.native_operations) <= set(self.identity.operations):
            raise InvalidGraph("synthetic native population differs")

    def validate_profile(self, path: pathlib.Path, encoding: str) -> None:
        if encoding != "RGBA u16":
            raise InvalidGraph("synthetic profile probe encoding differs")
        self.profile_paths.append(path)

    def import_pixels(self, payload: bytes, extent: Rect, encoding: str) -> object:
        native = object()
        self.imports.append((payload, extent, encoding, native))
        if self.cancel_on_import is not None:
            self.cancel_on_import.cancel()
        return native

    def compile_plan(
        self,
        plan: CompiledGraphPlan,
        buffers: dict[str, object],
        profiles: dict[ObjectId, pathlib.Path],
    ) -> object:
        native = object()
        self.plans.append(plan)
        self.plan_buffers.append(dict(buffers))
        self.plan_profiles.append(dict(profiles))
        if self.cancel_on_compile is not None:
            self.cancel_on_compile.cancel()
        return native

    def build_proxy(
        self,
        graph: object,
        *,
        level: int,
        expected_extent: Rect,
        encoding: str,
        scale_definition: object,
        sink_definition: object,
    ) -> object:
        native = object()
        self.proxy_builds.append(
            (level, expected_extent, encoding, scale_definition, sink_definition)
        )
        if self.cancel_on_proxy_build is not None:
            self.cancel_on_proxy_build.cancel()
        return native

    def read_buffer(self, buffer: object, rectangle: Rect, encoding: str) -> bytes:
        self.proxy_reads.append((buffer, rectangle, encoding))
        bytes_per_pixel = 1 if encoding == "Y u8" else 8
        return bytes((rectangle.x % 256,)) * (
            rectangle.width * rectangle.height * bytes_per_pixel
        )

    def render_tile(
        self,
        source: object,
        *,
        source_rectangle: Rect,
        destination_rectangle: Rect,
        encoding: str,
        source_definition: object,
        crop_definition: object,
        scale_definition: object,
    ) -> bytes:
        self.tile_renders.append(
            (source, source_rectangle, destination_rectangle, encoding)
        )
        if self.cancel_on_render is not None:
            self.cancel_on_render.cancel()
        bytes_per_pixel = 1 if encoding == "Y u8" else 8
        return bytes((destination_rectangle.x % 256,)) * (
            destination_rectangle.width
            * destination_rectangle.height
            * bytes_per_pixel
        )

    def duplicate_buffer(self, buffer: object) -> object:
        duplicate = object()
        self.mask_duplicates.append((buffer, duplicate))
        return duplicate

    def write_buffer(
        self,
        buffer: object,
        rectangle: Rect,
        encoding: str,
        payload: bytes,
    ) -> None:
        self.mask_writes.append((buffer, rectangle, encoding, payload))
        if self.cancel_on_mask_write is not None:
            self.cancel_on_mask_write.cancel()

    def release_buffer(self, buffer: object) -> None:
        self.lifecycle.append("buffer-release")
        self.released_buffers.append(buffer)

    def release_graph(self, graph: object) -> None:
        self.lifecycle.append("graph-release")
        self.released_graphs.append(graph)

    def smoke_test(self) -> None:
        pass

    def shutdown(self) -> None:
        self.lifecycle.append("shutdown")
        self.shutdown_count += 1


class CompilerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kilix-compiler-test-")
        self.root = pathlib.Path(self.temporary.name)
        self.group_record = self.root / "group.json"
        self.plugin_manifest = self.root / "plugins.json"
        self.group_record.write_bytes(GROUP_BYTES)
        self.plugin_manifest.write_bytes(PLUGIN_BYTES)
        self.registry = synthetic_registry()
        self.expected = EngineCompatibility(
            schema=EngineCompatibility.SCHEMA,
            package_group_id=PACKAGE_GROUP_ID,
            package_group_digest=ObjectId.from_bytes(GROUP_BYTES),
            gegl_version=GEGL_PACKAGE_VERSION,
            babl_version=BABL_PACKAGE_VERSION,
            python_gi_version=PYTHON_GI_PACKAGE_VERSION,
            gi_file_digest=GI_DIGEST,
            operation_count=EXPECTED_OPERATION_COUNT,
            operation_set_digest=self.registry.digest,
            plugin_tree_digest=ObjectId.from_bytes(PLUGIN_BYTES),
            working_format="RGBA u16",
            alpha_association=AlphaAssociation.STRAIGHT,
            mask_format="Y u8",
            mask_semantics="foreground-alpha",
            working_profile=PROFILE_B,
            conversion_policy=ConversionPolicy.RELATIVE_COLORIMETRIC,
            resampling_kernel="synthetic-nohalo",
            edge_mode="synthetic-clamp",
            tile_halos=(("synthetic-default", 0),),
            use_opencl=False,
            tile_cache_bytes=H0_TILE_CACHE_BYTES,
            swap_compression="fast",
            threads=2,
            deterministic_preset="f115-synthetic-h0-u16-v1",
            babl_tolerance="0.0",
        )
        configuration = RuntimeConfiguration(
            expected=self.expected,
            operation_registry=self.registry,
            package_group_record=self.group_record,
            plugin_tree_manifest=self.plugin_manifest,
            cache_root=self.root / "cache",
            runtime_root=self.root / "runtime",
        )
        self.backend = CompilerBackend(self.registry)
        runtime = ImageRuntime(
            configuration,
            native_loader=lambda: self.backend,
            process_guard=RuntimeProcessGuard(),
        )
        self.engine = Od7ImageEngine(runtime)
        self.environment = mock.patch.dict(
            os.environ,
            {"BABL_TOLERANCE": "0.0"},
            clear=True,
        )
        self.environment.start()
        self.capabilities = self.engine.start()
        self.cancel = CancelToken()

    def tearDown(self) -> None:
        if self.engine._capabilities is not None:
            self.engine.close()
        self.environment.stop()
        self.temporary.cleanup()

    def register_profiles(self) -> None:
        self.engine.register_profile(PROFILE_A_BYTES, PROFILE_A, cancel=self.cancel)
        self.engine.register_profile(PROFILE_B_BYTES, PROFILE_B, cancel=self.cancel)

    def import_graph_sources(self):
        pixel_payload = bytes(range(32))
        mask_payload = bytes((0, 85, 170, 255))
        pixel = self.engine.import_pixels(
            pixel_payload,
            extent=Rect(0, 0, 2, 2),
            spec=colour_spec(),
            revision=REVISION,
            cancel=self.cancel,
        )
        mask = self.engine.import_pixels(
            mask_payload,
            extent=Rect(0, 0, 2, 2),
            spec=PixelSpec.foreground_mask(),
            revision=REVISION,
            cancel=self.cancel,
        )
        self.engine.import_pixels(
            TEXT_RASTER_PAYLOAD,
            extent=Rect(0, 0, 2, 2),
            spec=colour_spec(),
            revision=REVISION,
            cancel=self.cancel,
        )
        return pixel, mask

    def compile_full_graph(self):
        self.register_profiles()
        pixel, mask = self.import_graph_sources()
        graph = full_graph(
            self.capabilities.compatibility_digest,
            pixels_digest=pixel.content_digest,
            mask_digest=mask.content_digest,
        )
        digest = self.engine.compile_graph(graph, cancel=self.cancel)
        return graph, digest, self.engine.compiled_plan(digest)


class BufferAndProfileTests(CompilerFixture):
    def test_profiles_are_digest_bound_private_and_removed_on_close(self) -> None:
        self.register_profiles()
        self.assertEqual(len(self.backend.profile_paths), 2)
        for path, payload in zip(
            self.backend.profile_paths,
            (PROFILE_A_BYTES, PROFILE_B_BYTES),
            strict=True,
        ):
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.parent.name, "profiles")
        paths = tuple(self.backend.profile_paths)
        self.engine.close()
        self.assertTrue(all(not path.exists() for path in paths))

    def test_pixel_import_uses_exact_h0_encodings_and_opaque_refs(self) -> None:
        self.register_profiles()
        pixel, mask = self.import_graph_sources()
        self.assertEqual(tuple(item[2] for item in self.backend.imports), (
            "RGBA u16",
            "Y u8",
            "RGBA u16",
        ))
        self.assertTrue(pixel.token.startswith("od7:"))
        self.assertTrue(mask.token.startswith("od7:"))
        self.assertNotIn("Gegl", repr(pixel))

    def test_import_refuses_unregistered_profile_and_wrong_alpha(self) -> None:
        with self.assertRaises(DecodeRefusal):
            self.engine.import_pixels(
                bytes(8),
                extent=Rect(0, 0, 1, 1),
                spec=colour_spec(),
                revision=REVISION,
                cancel=self.cancel,
            )
        self.register_profiles()
        with self.assertRaises(UnsupportedOperation):
            self.engine.import_pixels(
                bytes(8),
                extent=Rect(0, 0, 1, 1),
                spec=PixelSpec.colour(
                    PixelFormat.RGBA_U16,
                    PROFILE_A,
                    alpha_association=AlphaAssociation.PREMULTIPLIED,
                ),
                revision=REVISION,
                cancel=self.cancel,
            )

    def test_cancelled_import_releases_native_buffer_and_publishes_zero_refs(self) -> None:
        self.register_profiles()
        token = CancelToken()
        self.backend.cancel_on_import = token
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.import_pixels(
                bytes(8),
                extent=Rect(0, 0, 1, 1),
                spec=colour_spec(),
                revision=REVISION,
                cancel=token,
            )
        self.assertEqual(len(self.backend.released_buffers), 1)
        self.assertEqual(len(self.engine._buffers), 0)

    def test_sparse_mask_edit_duplicates_and_writes_only_the_changed_tile(self) -> None:
        self.register_profiles()
        _, mask = self.import_graph_sources()
        revised = self.engine.edit_mask(
            mask,
            (
                MaskTileUpdate(
                    Rect(0, 0, 2, 2),
                    ObjectId.from_bytes(bytes((0, 85, 170, 255))),
                    bytes((255, 170, 85, 0)),
                ),
            ),
            new_revision=RevisionId("44444444-4444-4444-8444-444444444444"),
            cancel=self.cancel,
        )
        self.assertNotEqual(revised.content_digest, mask.content_digest)
        self.assertEqual(len(self.backend.mask_duplicates), 1)
        self.assertEqual(len(self.backend.mask_writes), 1)
        self.assertEqual(self.backend.mask_writes[0][1], Rect(0, 0, 2, 2))
        self.assertEqual(self.backend.mask_writes[0][2], "Y u8")

    def test_cancelled_mask_write_releases_duplicate_and_publishes_zero_buffers(self) -> None:
        self.register_profiles()
        _, mask = self.import_graph_sources()
        token = CancelToken()
        self.backend.cancel_on_mask_write = token
        before_buffers = len(self.engine._buffers)
        before_releases = len(self.backend.released_buffers)
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.edit_mask(
                mask,
                (
                    MaskTileUpdate(
                        Rect(0, 0, 2, 2),
                        ObjectId.from_bytes(bytes((0, 85, 170, 255))),
                        bytes((255, 255, 255, 255)),
                    ),
                ),
                new_revision=RevisionId("55555555-5555-4555-8555-555555555555"),
                cancel=token,
            )
        self.assertEqual(len(self.engine._buffers), before_buffers)
        self.assertEqual(len(self.backend.released_buffers), before_releases + 1)


class ClosedCompilerTests(CompilerFixture):
    def test_all_nine_graph_families_expand_to_a_closed_native_plan(self) -> None:
        graph, digest, plan = self.compile_full_graph()
        self.assertEqual({item.kind for item in graph.nodes}, set(GraphNodeKind))
        self.assertEqual(digest, graph.digest)
        self.assertEqual(len(plan.nodes), 13)
        self.assertEqual(plan.output_plan_id, "destination__crop")
        self.assertEqual(self.backend.plans, [plan])
        self.assertEqual(len(self.backend.plan_buffers[0]), 3)
        self.assertEqual(len(self.backend.plan_profiles[0]), 2)
        self.assertTrue(
            any(
                isinstance(property_value.value, BufferBinding)
                for node in plan.nodes
                for property_value in node.properties
            )
        )
        self.assertTrue(
            any(
                isinstance(property_value.value, ProfileBinding)
                for node in plan.nodes
                for property_value in node.properties
            )
        )

    def test_hostile_adjustment_property_is_refused_before_native_compile(self) -> None:
        self.register_profiles()
        pixel, mask = self.import_graph_sources()
        graph = full_graph(
            self.capabilities.compatibility_digest,
            pixels_digest=pixel.content_digest,
            mask_digest=mask.content_digest,
        )
        adjustment = graph.nodes[6]
        hostile = dataclasses.replace(
            adjustment,
            parameters=AdjustmentParameters(
                Adjustment(
                    AdjustmentId.CONTRAST,
                    (Parameter("operation", "gegl:load"),),
                )
            ),
        )
        malformed = dataclasses.replace(
            graph,
            nodes=(*graph.nodes[:6], hostile, *graph.nodes[7:]),
        )
        with self.assertRaises(InvalidGraph):
            self.engine.compile_graph(malformed, cancel=self.cancel)
        self.assertEqual(self.backend.plans, [])

    def test_graph_halo_is_bound_to_registry_fixture_identity(self) -> None:
        self.register_profiles()
        pixel, mask = self.import_graph_sources()
        graph = full_graph(
            self.capabilities.compatibility_digest,
            pixels_digest=pixel.content_digest,
            mask_digest=mask.content_digest,
        )
        adjustment = dataclasses.replace(graph.nodes[6], halo_pixels=0)
        malformed = dataclasses.replace(
            graph,
            nodes=(*graph.nodes[:6], adjustment, *graph.nodes[7:]),
        )
        with self.assertRaises(InvalidGraph):
            self.engine.compile_graph(malformed, cancel=self.cancel)

    def test_cancelled_compile_releases_graph_and_publishes_zero_results(self) -> None:
        self.register_profiles()
        pixel, mask = self.import_graph_sources()
        graph = full_graph(
            self.capabilities.compatibility_digest,
            pixels_digest=pixel.content_digest,
            mask_digest=mask.content_digest,
        )
        token = CancelToken()
        self.backend.cancel_on_compile = token
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.compile_graph(graph, cancel=token)
        self.assertEqual(len(self.backend.released_graphs), 1)
        with self.assertRaises(InvalidGraph):
            self.engine.compiled_plan(graph.digest)

    def test_slice_four_builds_and_caches_one_complete_native_proxy_level(self) -> None:
        graph, digest, _ = self.compile_full_graph()
        plan = ProxyBuildPlan(
            digest,
            Rect(0, 0, 2, 2),
            1,
            graph.output_spec,
            graph.revision,
            graph.compatibility_digest,
        )
        requests = plan.requests()
        first = self.engine.build_proxy(requests, cancel=self.cancel)
        second = self.engine.build_proxy(requests, cancel=self.cancel)
        manifest = ProxyCache().publish(
            plan,
            first,
            current_revision=graph.revision,
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].destination, Rect(0, 0, 1, 1))
        self.assertEqual(len(manifest.tiles), 1)
        self.assertEqual(len(self.backend.proxy_builds), 1)
        self.assertEqual(len(self.backend.proxy_reads), 1)
        released_before = len(self.backend.released_buffers)
        self.assertEqual(self.engine.invalidate_proxies((digest,)), 1)
        self.assertEqual(len(self.backend.released_buffers), released_before + 1)
        self.assertEqual(self.engine.invalidate_proxies((digest,)), 0)
        level_zero = TileRequest(
            digest,
            Rect(0, 0, 2, 2),
            Rect(0, 0, 1, 1),
            0,
            graph.output_spec,
            graph.revision,
        )
        tile = self.engine.render_tile(level_zero, cancel=self.cancel)
        self.assertEqual(len(tile.owned_bytes or b""), 8)
        self.assertEqual(len(self.backend.tile_renders), 1)
        exported = self.engine.export_tiles((level_zero,), cancel=self.cancel)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].destination, level_zero.destination)
        self.assertIsInstance(self.engine, ImageEngine)

    def test_cancelled_native_tile_call_publishes_zero_results(self) -> None:
        graph, digest, _ = self.compile_full_graph()
        token = CancelToken()
        self.backend.cancel_on_render = token
        request = TileRequest(
            digest,
            Rect(0, 0, 2, 2),
            Rect(0, 0, 2, 2),
            0,
            graph.output_spec,
            graph.revision,
        )
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.render_tile(request, cancel=token)
        self.assertEqual(len(self.backend.tile_renders), 1)

    def test_cancelled_full_resolution_batch_returns_zero_tile_tuples(self) -> None:
        graph, digest, _ = self.compile_full_graph()
        requests = tuple(
            TileRequest(
                digest,
                Rect(x, 0, 1, 2),
                Rect(x, 0, 1, 2),
                0,
                graph.output_spec,
                graph.revision,
            )
            for x in range(2)
        )
        token = CancelToken()
        self.backend.cancel_on_render = token
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.export_tiles(requests, cancel=token)
        self.assertEqual(len(self.backend.tile_renders), 1)

    def test_cancelled_native_proxy_build_releases_buffer_and_publishes_zero_results(
        self,
    ) -> None:
        graph, digest, _ = self.compile_full_graph()
        token = CancelToken()
        self.backend.cancel_on_proxy_build = token
        requests = ProxyBuildPlan(
            digest,
            Rect(0, 0, 2, 2),
            1,
            graph.output_spec,
            graph.revision,
            graph.compatibility_digest,
        ).requests()
        released_before = len(self.backend.released_buffers)
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.build_proxy(requests, cancel=token)
        self.assertEqual(len(self.backend.released_buffers), released_before + 1)
        self.assertEqual(len(self.engine._proxies), 0)
        self.assertEqual(len(self.engine._proxy_results), 0)

    def test_close_releases_all_native_objects_before_runtime_shutdown(self) -> None:
        self.compile_full_graph()
        self.engine.close()
        self.assertEqual(
            self.backend.lifecycle,
            [
                "graph-release",
                "buffer-release",
                "buffer-release",
                "buffer-release",
                "shutdown",
            ],
        )
        self.assertEqual(len(self.backend.released_graphs), 1)
        self.assertEqual(len(self.backend.released_buffers), 3)
        self.assertEqual(self.backend.shutdown_count, 1)


class IccBoundaryTests(unittest.TestCase):
    def test_product_uses_zero_direct_binary_babl_icc_calls(self) -> None:
        runtime = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/kilix_image_shop/engine/runtime.py"
        ).read_text()
        self.assertNotIn("space_from_" + "icc", runtime)
        self.assertIn('graph.create_child("gegl:cast-space")', runtime)
        self.assertIn('graph.create_child("gegl:convert-space")', runtime)

    def test_product_has_one_blit_call_site_and_zero_processor_call_sites(self) -> None:
        runtime = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src/kilix_image_shop/engine/runtime.py"
        ).read_text()
        self.assertEqual(runtime.count(".blit_" + "buffer("), 1)
        self.assertNotIn("new_" + "processor", runtime)


if __name__ == "__main__":
    unittest.main()
