from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import PixelLayer, TextLayer
from kilix_image_shop.engine.api import (
    CancelToken,
    CancelledOrStaleWork,
    FakeImageEngine,
    GraphNodeKind,
    PixelFormat,
    PixelSpec,
)
from kilix_image_shop.render.compositor import (
    CompositionRequest,
    Compositor,
    assemble_tiles,
)
from kilix_image_shop.render.plan import derive_render_plan
from kilix_image_shop.render.proxy import ProxyCache
from kilix_image_shop.render.scheduler import TileScheduler, partition_tiles

from domain_fixtures import empty_document, layer_id, object_id, sample_document


def small_render_document():
    profile_payload = b"synthetic-icc-profile"
    profile = ObjectId.from_bytes(profile_payload)
    pixel_payload = bytes(range(32)) * 4
    pixel_digest = ObjectId.from_bytes(pixel_payload)
    base = empty_document()
    compatibility = dataclasses.replace(
        base.engine_compatibility,
        working_profile=profile,
    )
    colour = dataclasses.replace(base.colour, working_profile=profile)
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
        colour=colour,
        engine_compatibility=compatibility,
        assets=(asset,),
        root_layer_ids=(identity,),
        layers=(layer,),
    )
    return document, profile_payload, pixel_payload


def started_engine(document, profile_payload: bytes, pixel_payload: bytes):
    engine = FakeImageEngine(
        compatibility_digest=document.engine_compatibility.digest,
    )
    capabilities = engine.start()
    cancel = CancelToken()
    profile = document.colour.working_profile
    engine.register_profile(profile_payload, profile, cancel=cancel)
    engine.import_pixels(
        pixel_payload,
        extent=Rect(0, 0, 4, 4),
        spec=PixelSpec.colour(
            PixelFormat.RGBA_U16,
            profile,
            alpha_association=document.engine_compatibility.alpha_association,
        ),
        revision=document.revision_id,
        cancel=cancel,
    )
    return engine, capabilities


class RenderPlanTests(unittest.TestCase):
    def test_plan_projects_all_nine_closed_graph_families_deterministically(self) -> None:
        document = sample_document()
        assets = tuple(
            dataclasses.replace(asset, profile_digest=object_id("c"))
            if asset.digest == object_id("6")
            else asset
            for asset in document.assets
        )
        document = dataclasses.replace(document, assets=assets)
        first = derive_render_plan(document)
        second = derive_render_plan(document)
        self.assertEqual(first, second)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual({node.kind for node in first.graph.nodes}, set(GraphNodeKind))
        self.assertEqual(len({node.kind for node in first.graph.nodes}), 9)
        self.assertEqual(first.compatibility_digest, document.engine_compatibility.digest)
        self.assertEqual(first.output_bounds, document.canvas.bounds)
        self.assertEqual(first.dependency.graph_digest, first.digest)
        self.assertIn(object_id("c"), first.object_ids)

    def test_hidden_subtree_values_do_not_enter_the_visible_render_projection(self) -> None:
        document = sample_document()
        hidden_layers = tuple(
            dataclasses.replace(layer, visible=False)
            if isinstance(layer, TextLayer)
            else layer
            for layer in document.layers
        )
        hidden = derive_render_plan(dataclasses.replace(document, layers=hidden_layers))
        visible = derive_render_plan(document)
        self.assertNotEqual(hidden.digest, visible.digest)
        self.assertNotIn(layer_id(3), hidden.layer_ids)
        self.assertNotIn(object_id("a"), hidden.object_ids)
        self.assertNotIn(object_id("7"), hidden.object_ids)
        self.assertNotIn(GraphNodeKind.TEXT_SOURCE, {item.kind for item in hidden.graph.nodes})

    def test_output_geometry_and_render_values_change_plan_identity(self) -> None:
        document, _, _ = small_render_document()
        full = derive_render_plan(document)
        cropped = derive_render_plan(document, output_bounds=Rect(0, 0, 2, 2))
        renamed = dataclasses.replace(
            document,
            layers=(dataclasses.replace(document.layers[0], name="presentation only"),),
        )
        self.assertNotEqual(full.digest, cropped.digest)
        self.assertEqual(full.digest, derive_render_plan(renamed).digest)
        changed = dataclasses.replace(
            document,
            layers=(dataclasses.replace(document.layers[0], opacity_u16=32768),),
        )
        self.assertNotEqual(full.digest, derive_render_plan(changed).digest)


class CompositorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document, profile, pixels = small_render_document()
        self.engine, _ = started_engine(self.document, profile, pixels)
        self.plan = derive_render_plan(self.document)
        self.scheduler = TileScheduler(32)
        self.proxies = ProxyCache()
        self.compositor = Compositor(self.engine, self.scheduler, self.proxies)
        self.current = lambda: self.document.revision_id

    def tearDown(self) -> None:
        self.engine.close()

    def test_all_three_proxy_levels_publish_before_zoom_out_composition(self) -> None:
        levels = self.compositor.build_proxies(
            self.plan,
            current_revision=self.current,
            cancel=CancelToken(),
        )
        self.assertEqual(levels, (1, 2, 3))
        self.assertTrue(
            all(self.proxies.manifest(self.plan.digest, level) is not None for level in levels)
        )
        image = self.compositor.compose(
            self.plan,
            CompositionRequest(
                source=Rect(0, 0, 4, 4),
                destination=Rect(0, 0, 8, 8),
                zoom_ratio=0.5,
            ),
            current_revision=self.current,
            cancel=CancelToken(),
        )
        self.assertEqual(image.level, 1)
        self.assertEqual(image.source, Rect(0, 0, 2, 2))
        self.assertNotEqual(image.source, self.document.canvas.bounds)
        self.assertEqual(len(image.payload), 8 * 8 * 8)

    def test_tiled_and_single_pass_fake_composition_are_byte_identical(self) -> None:
        self.compositor.compile(
            self.plan,
            current_revision=self.current,
            cancel=CancelToken(),
        )
        source = Rect(0, 0, 4, 4)
        destination = Rect(0, 0, 6, 4)
        tiled_requests = partition_tiles(
            graph_digest=self.plan.digest,
            source=source,
            destination=destination,
            level=0,
            spec=self.plan.output_spec,
            revision=self.plan.revision,
            maximum_width=3,
            maximum_height=2,
        )
        single_request = partition_tiles(
            graph_digest=self.plan.digest,
            source=source,
            destination=destination,
            level=0,
            spec=self.plan.output_spec,
            revision=self.plan.revision,
            maximum_width=6,
            maximum_height=4,
        )
        tiled = tuple(
            self.engine.render_tile(item, cancel=CancelToken()) for item in tiled_requests
        )
        single = tuple(
            self.engine.render_tile(item, cancel=CancelToken()) for item in single_request
        )
        self.assertEqual(len(tiled), 4)
        self.assertEqual(len(single), 1)
        self.assertEqual(
            assemble_tiles(tiled, destination, self.plan.output_spec, cancel=CancelToken()),
            assemble_tiles(single, destination, self.plan.output_spec, cancel=CancelToken()),
        )

    def test_large_viewport_uses_multiple_bounded_tiles_and_one_publication(self) -> None:
        image = self.compositor.compose(
            self.plan,
            CompositionRequest(
                source=Rect(0, 0, 4, 4),
                destination=Rect(0, 0, 1921, 2),
                zoom_ratio=1.0,
            ),
            current_revision=self.current,
            cancel=CancelToken(),
        )
        self.assertEqual(image.tile_count, 2)
        self.assertEqual(len(image.payload), 1921 * 2 * 8)
        self.assertEqual(self.scheduler.queued_batches, 0)

    def test_cancelled_or_stale_work_publishes_zero_viewport_values(self) -> None:
        cancelled = CancelToken()
        cancelled.cancel()
        with self.assertRaises(CancelledOrStaleWork):
            self.compositor.compose(
                self.plan,
                CompositionRequest(Rect(0, 0, 4, 4), Rect(0, 0, 4, 4), 1.0),
                current_revision=self.current,
                cancel=cancelled,
            )
        self.assertEqual(self.scheduler.queued_batches, 0)
        with self.assertRaises(CancelledOrStaleWork):
            self.compositor.compose(
                self.plan,
                CompositionRequest(Rect(0, 0, 4, 4), Rect(0, 0, 4, 4), 1.0),
                current_revision=lambda: RevisionId(
                    "ffffffff-ffff-4fff-8fff-ffffffffffff"
                ),
                cancel=CancelToken(),
            )
        self.assertEqual(self.scheduler.queued_batches, 0)


class RenderDependencyTests(unittest.TestCase):
    def test_five_render_modules_are_native_and_filesystem_free(self) -> None:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "render"
        )
        modules = tuple(
            sorted(path.name for path in root.glob("*.py") if path.name != "__init__.py")
        )
        self.assertEqual(
            modules,
            ("compositor.py", "graph.py", "plan.py", "proxy.py", "scheduler.py"),
        )
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
                        ("gi", "pathlib", "os", "kilix_image_shop.store")
                    )
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
