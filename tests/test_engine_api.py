from __future__ import annotations

import ast
import dataclasses
import pathlib
import threading
import unittest

from kilix_image_shop.domain.color import AlphaAssociation, ConversionPolicy
from kilix_image_shop.domain.geometry import AffineTransform, Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.domain.layers import Adjustment, AdjustmentId, BlendMode, Parameter
from kilix_image_shop.engine.api import (
    ENGINE_FAILURE_TYPES,
    AdjustmentParameters,
    AffineTransformCropParameters,
    CancelToken,
    CancelledOrStaleWork,
    ColourConversionParameters,
    DecodeRefusal,
    DestinationCropScaleParameters,
    EngineFailureCode,
    FakeImageEngine,
    GraphNodeKind,
    GraphNodeSpec,
    GraphSpec,
    ImageEngine,
    IncompatibleRuntime,
    InternalEngineFailure,
    InvalidGraph,
    MaskParameters,
    MaskSemantics,
    OpacityBlendParameters,
    OrderedGroupParameters,
    PixelFormat,
    PixelSourceParameters,
    PixelSpec,
    ResourceExhaustion,
    TextSourceParameters,
    TileRequest,
    UnsupportedOperation,
)
from kilix_image_shop.engine.formats import RenderTier, TierFormatPolicy


ROOT = pathlib.Path(__file__).resolve().parents[1]
REVISION = RevisionId("00000000-0000-4000-8000-000000000115")
STALE_REVISION = RevisionId("00000000-0000-4000-8000-000000000116")
PROFILE_A_BYTES = b"synthetic ICC profile A"
PROFILE_B_BYTES = b"synthetic ICC profile B"
PROFILE_A = ObjectId.from_bytes(PROFILE_A_BYTES)
PROFILE_B = ObjectId.from_bytes(PROFILE_B_BYTES)
TEXT_RASTER_PAYLOAD = bytes(reversed(range(32)))
TEXT_RASTER_DIGEST = ObjectId.from_bytes(TEXT_RASTER_PAYLOAD)


def colour_spec(profile: ObjectId = PROFILE_A) -> PixelSpec:
    return PixelSpec.colour(PixelFormat.RGBA_U16, profile)


def full_graph(
    compatibility_digest: ObjectId,
    *,
    pixels_digest: ObjectId,
    mask_digest: ObjectId,
) -> GraphSpec:
    colour_a = colour_spec()
    colour_b = colour_spec(PROFILE_B)
    mask = PixelSpec.foreground_mask()
    bounds = Rect(0, 0, 2, 2)
    nodes = (
        GraphNodeSpec(
            "pixels",
            GraphNodeKind.PIXEL_SOURCE,
            (),
            PixelSourceParameters(pixels_digest, bounds),
            colour_a,
        ),
        GraphNodeSpec(
            "text",
            GraphNodeKind.TEXT_SOURCE,
            (),
            TextSourceParameters(
                ObjectId("c" * 64),
                ObjectId("d" * 64),
                TEXT_RASTER_DIGEST,
                bounds,
            ),
            colour_a,
        ),
        GraphNodeSpec(
            "transform",
            GraphNodeKind.AFFINE_TRANSFORM_CROP,
            ("text",),
            AffineTransformCropParameters(AffineTransform(), bounds),
            colour_a,
        ),
        GraphNodeSpec(
            "blend",
            GraphNodeKind.OPACITY_BLEND,
            ("pixels", "transform"),
            OpacityBlendParameters(65535, BlendMode.NORMAL),
            colour_a,
        ),
        GraphNodeSpec(
            "mask-source",
            GraphNodeKind.PIXEL_SOURCE,
            (),
            PixelSourceParameters(mask_digest, bounds),
            mask,
        ),
        GraphNodeSpec(
            "masked",
            GraphNodeKind.MASK,
            ("blend", "mask-source"),
            MaskParameters(),
            colour_a,
        ),
        GraphNodeSpec(
            "adjusted",
            GraphNodeKind.ADJUSTMENT,
            ("masked",),
            AdjustmentParameters(
                Adjustment(
                    AdjustmentId.CONTRAST,
                    (Parameter("amount", 0.25),),
                )
            ),
            colour_a,
            halo_pixels=1,
        ),
        GraphNodeSpec(
            "group",
            GraphNodeKind.ORDERED_GROUP,
            ("adjusted",),
            OrderedGroupParameters(),
            colour_a,
        ),
        GraphNodeSpec(
            "converted",
            GraphNodeKind.COLOUR_CONVERSION,
            ("group",),
            ColourConversionParameters(
                PROFILE_A,
                PROFILE_B,
                ConversionPolicy.RELATIVE_COLORIMETRIC,
            ),
            colour_b,
        ),
        GraphNodeSpec(
            "destination",
            GraphNodeKind.DESTINATION_CROP_SCALE,
            ("converted",),
            DestinationCropScaleParameters(bounds, bounds),
            colour_b,
        ),
    )
    return GraphSpec(REVISION, compatibility_digest, nodes, "destination")


class PixelContractTests(unittest.TestCase):
    def test_colour_and_mask_specs_are_explicit_and_immutable(self) -> None:
        colour = colour_spec()
        mask = PixelSpec.foreground_mask()
        self.assertEqual(colour.pixel_format, PixelFormat.RGBA_U16)
        self.assertEqual(mask.mask_semantics, MaskSemantics.FOREGROUND_ALPHA)
        self.assertIsNone(mask.profile_digest)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            colour.pixel_format = PixelFormat.RGBA_FLOAT  # type: ignore[misc]

    def test_invalid_profile_alpha_and_mask_combinations_are_refused(self) -> None:
        with self.assertRaises(InvalidGraph):
            PixelSpec(
                PixelFormat.Y_U8,
                AlphaAssociation.STRAIGHT,
                None,
                MaskSemantics.FOREGROUND_ALPHA,
            )
        with self.assertRaises(InvalidGraph):
            PixelSpec(
                PixelFormat.RGBA_U16,
                AlphaAssociation.STRAIGHT,
                None,
            )
        with self.assertRaises(InvalidGraph):
            PixelSpec(
                PixelFormat.RGBA_U16,
                AlphaAssociation.STRAIGHT,
                PROFILE_A,
                MaskSemantics.FOREGROUND_ALPHA,
            )

    def test_h0_admits_u16_colour_and_y_u8_mask_but_not_float(self) -> None:
        policy = TierFormatPolicy.h0()
        self.assertEqual(policy.tier, RenderTier.H0)
        self.assertEqual(policy.expected_byte_count(2, 3, colour_spec()), 48)
        self.assertEqual(policy.expected_byte_count(2, 3, policy.mask_spec), 6)
        policy.validate_payload(bytes(48), 2, 3, colour_spec())
        with self.assertRaises(UnsupportedOperation):
            TierFormatPolicy(RenderTier.H0, PixelFormat.RGBA_FLOAT)
        with self.assertRaises(UnsupportedOperation):
            policy.validate(PixelSpec.colour(PixelFormat.RGBA_FLOAT, PROFILE_A))
        with self.assertRaises(InvalidGraph):
            policy.validate_payload(bytes(47), 2, 3, colour_spec())


class GraphContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compatibility = ObjectId("f" * 64)
        self.graph = full_graph(
            self.compatibility,
            pixels_digest=ObjectId("1" * 64),
            mask_digest=ObjectId("2" * 64),
        )

    def test_graph_covers_exactly_nine_closed_families(self) -> None:
        self.assertEqual({node.kind for node in self.graph.nodes}, set(GraphNodeKind))
        self.assertEqual(len(GraphNodeKind), 9)
        self.assertTrue(self.graph.canonical_bytes().endswith(b"\n"))
        self.assertEqual(self.graph.digest, ObjectId.from_bytes(self.graph.canonical_bytes()))
        self.assertNotIn(b"gegl:", self.graph.canonical_bytes())

    def test_native_operation_and_property_strings_have_no_graph_channel(self) -> None:
        with self.assertRaises(InvalidGraph):
            GraphNodeSpec(
                "hostile",
                "gegl:load",  # type: ignore[arg-type]
                (),
                PixelSourceParameters(ObjectId("1" * 64), Rect(0, 0, 1, 1)),
                colour_spec(),
            )
        with self.assertRaises(InvalidGraph):
            GraphNodeSpec(
                "hostile",
                GraphNodeKind.PIXEL_SOURCE,
                (),
                {"operation": "gegl:load"},  # type: ignore[arg-type]
                colour_spec(),
            )

    def test_graph_refuses_forward_edges_disconnected_nodes_and_wrong_output(self) -> None:
        nodes = self.graph.nodes
        with self.assertRaises(InvalidGraph):
            GraphSpec(
                REVISION,
                self.compatibility,
                (nodes[0], nodes[2], nodes[1], *nodes[3:]),
                "destination",
            )
        unused = GraphNodeSpec(
            "unused",
            GraphNodeKind.TEXT_SOURCE,
            (),
            TextSourceParameters(
                ObjectId("7" * 64),
                ObjectId("8" * 64),
                ObjectId("9" * 64),
                Rect(0, 0, 1, 1),
            ),
            colour_spec(PROFILE_B),
        )
        with self.assertRaises(InvalidGraph):
            GraphSpec(
                REVISION,
                self.compatibility,
                (*nodes[:-1], unused, nodes[-1]),
                "destination",
            )
        with self.assertRaises(InvalidGraph):
            GraphSpec(REVISION, self.compatibility, nodes[:-1], "converted")

    def test_failure_surface_has_eight_stable_unique_families(self) -> None:
        self.assertEqual(len(ENGINE_FAILURE_TYPES), 8)
        self.assertEqual({item.code for item in ENGINE_FAILURE_TYPES}, set(EngineFailureCode))

    def test_destination_tiles_are_bounded_on_both_axes(self) -> None:
        common = {
            "graph_digest": self.graph.digest,
            "source": Rect(0, 0, 1, 1),
            "level": 0,
            "spec": self.graph.output_spec,
            "revision": REVISION,
        }
        TileRequest(destination=Rect(0, 0, 1920, 1080), **common)
        for destination in (Rect(0, 0, 1921, 1), Rect(0, 0, 1, 1081)):
            with self.subTest(destination=destination), self.assertRaises(
                ResourceExhaustion
            ):
                TileRequest(destination=destination, **common)


class FakeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FakeImageEngine()
        self.capabilities = self.engine.start()
        self.cancel = CancelToken()
        self.engine.register_profile(PROFILE_A_BYTES, PROFILE_A, cancel=self.cancel)
        self.engine.register_profile(PROFILE_B_BYTES, PROFILE_B, cancel=self.cancel)
        self.pixel_payload = bytes(range(32))
        self.mask_payload = bytes((0, 85, 170, 255))
        self.pixel_buffer = self.engine.import_pixels(
            self.pixel_payload,
            extent=Rect(0, 0, 2, 2),
            spec=colour_spec(),
            revision=REVISION,
            cancel=self.cancel,
        )
        self.mask_buffer = self.engine.import_pixels(
            self.mask_payload,
            extent=Rect(0, 0, 2, 2),
            spec=PixelSpec.foreground_mask(),
            revision=REVISION,
            cancel=self.cancel,
        )
        self.text_buffer = self.engine.import_pixels(
            TEXT_RASTER_PAYLOAD,
            extent=Rect(0, 0, 2, 2),
            spec=colour_spec(),
            revision=REVISION,
            cancel=self.cancel,
        )
        self.graph = full_graph(
            self.capabilities.compatibility_digest,
            pixels_digest=self.pixel_buffer.content_digest,
            mask_digest=self.mask_buffer.content_digest,
        )
        self.graph_digest = self.engine.compile_graph(self.graph, cancel=self.cancel)

    def tearDown(self) -> None:
        self.engine.close()

    def request(self, *, level: int = 0, revision: RevisionId = REVISION) -> TileRequest:
        return TileRequest(
            self.graph_digest,
            Rect(0, 0, 2, 2),
            Rect(0, 0, 2, 2) if level == 0 else Rect(0, 0, 1, 1),
            level,
            self.graph.output_spec,
            revision,
        )

    def test_fake_conforms_and_import_identity_is_deterministic(self) -> None:
        self.assertIsInstance(self.engine, ImageEngine)
        repeated = self.engine.import_pixels(
            self.pixel_payload,
            extent=Rect(0, 0, 2, 2),
            spec=colour_spec(),
            revision=REVISION,
            cancel=self.cancel,
        )
        self.assertEqual(repeated, self.pixel_buffer)
        self.assertEqual(self.pixel_buffer.content_digest, ObjectId.from_bytes(self.pixel_payload))

    def test_fake_tile_output_is_owned_exact_and_deterministic(self) -> None:
        request = self.request()
        first = self.engine.render_tile(request, cancel=self.cancel)
        second = self.engine.render_tile(request, cancel=self.cancel)
        self.assertEqual(first, second)
        self.assertEqual(len(first.owned_bytes or b""), 32)
        self.assertIsNone(first.buffer_ref)
        self.assertEqual(first.elapsed_ns, 0)

    def test_proxy_and_export_paths_preserve_request_order(self) -> None:
        proxies = (self.request(level=1), self.request(level=2), self.request(level=3))
        proxy_results = tuple(
            self.engine.build_proxy((item,), cancel=self.cancel)[0]
            for item in proxies
        )
        export_results = self.engine.export_tiles((self.request(),), cancel=self.cancel)
        self.assertEqual(tuple(item.level for item in proxy_results), (1, 2, 3))
        self.assertEqual(tuple(item.level for item in export_results), (0,))
        with self.assertRaises(InvalidGraph):
            self.engine.build_proxy((self.request(),), cancel=self.cancel)
        with self.assertRaises(InvalidGraph):
            self.engine.build_proxy(proxies, cancel=self.cancel)
        self.assertEqual(self.engine.invalidate_proxies((self.graph_digest,)), 3)
        self.assertEqual(self.engine.invalidate_proxies((self.graph_digest,)), 0)

    def test_cancelled_stale_and_uncompiled_work_returns_no_result(self) -> None:
        cancelled = CancelToken()
        cancelled.cancel()
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.render_tile(self.request(), cancel=cancelled)
        with self.assertRaises(CancelledOrStaleWork):
            self.engine.render_tile(
                self.request(revision=STALE_REVISION),
                cancel=self.cancel,
            )
        with self.assertRaises(InvalidGraph):
            self.engine.render_tile(self.request(level=1), cancel=self.cancel)
        unknown = TileRequest(
            ObjectId("0" * 64),
            Rect(0, 0, 1, 1),
            Rect(0, 0, 1, 1),
            0,
            self.graph.output_spec,
            REVISION,
        )
        with self.assertRaises(InvalidGraph):
            self.engine.render_tile(unknown, cancel=self.cancel)

    def test_h0_fake_refuses_float_and_malformed_decode(self) -> None:
        with self.assertRaises(UnsupportedOperation):
            self.engine.import_pixels(
                bytes(64),
                extent=Rect(0, 0, 1, 1),
                spec=PixelSpec.colour(PixelFormat.RGBA_FLOAT, PROFILE_A),
                revision=REVISION,
                cancel=self.cancel,
            )
        with self.assertRaises(DecodeRefusal):
            self.engine.import_pixels(
                bytes(31),
                extent=Rect(0, 0, 2, 2),
                spec=colour_spec(),
                revision=REVISION,
                cancel=self.cancel,
            )

    def test_compile_binds_source_digest_revision_extent_and_spec(self) -> None:
        source = self.graph.nodes[0]
        malformed_source = dataclasses.replace(
            source,
            parameters=PixelSourceParameters(
                self.pixel_buffer.content_digest,
                Rect(0, 0, 1, 1),
            ),
        )
        malformed = dataclasses.replace(
            self.graph,
            nodes=(malformed_source, *self.graph.nodes[1:]),
        )
        with self.assertRaises(InvalidGraph):
            self.engine.compile_graph(malformed, cancel=self.cancel)

    def test_exactly_one_owner_thread_can_access_engine_state(self) -> None:
        failures: list[BaseException] = []

        def access_from_non_owner() -> None:
            try:
                self.engine.render_tile(self.request(), cancel=CancelToken())
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=access_from_non_owner)
        worker.start()
        worker.join()
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], InternalEngineFailure)
        self.assertEqual(
            getattr(failures[0], "diagnostic_ref", None),
            "fake.owner-thread",
        )

    def test_closed_engine_cannot_restart(self) -> None:
        self.engine.close()
        with self.assertRaises(IncompatibleRuntime):
            self.engine.start()
        self.engine = FakeImageEngine()
        self.engine.start()


class DependencyBoundaryTests(unittest.TestCase):
    def test_engine_neutral_modules_import_zero_gi_modules(self) -> None:
        for relative in (
            "src/kilix_image_shop/engine/api.py",
            "src/kilix_image_shop/engine/formats.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(), filename=relative)
            imports = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertFalse(
                {name for name in imports if name == "gi" or name.startswith("gi.")},
                relative,
            )


if __name__ == "__main__":
    unittest.main()
