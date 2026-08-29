from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.engine.api import (
    CancelledOrStaleWork,
    InvalidGraph,
    PixelFormat,
    PixelSpec,
    TileResult,
)
from kilix_image_shop.render.graph import (
    GraphDependency,
    InvalidationRequest,
    plan_invalidation,
)
from kilix_image_shop.render.proxy import (
    PROXY_ALGORITHM_VERSION,
    PROXY_LEVELS,
    ProxyBuildPlan,
    ProxyCache,
    ProxyKey,
    proxy_extent,
    select_proxy_level,
)


PLAN_A = ObjectId("1" * 64)
PLAN_B = ObjectId("2" * 64)
PROFILE = ObjectId("3" * 64)
COMPATIBILITY = ObjectId("4" * 64)
OBJECT_A = ObjectId("5" * 64)
REVISION_A = RevisionId("11111111-1111-4111-8111-111111111111")
REVISION_B = RevisionId("22222222-2222-4222-8222-222222222222")
REVISION_C = RevisionId("33333333-3333-4333-8333-333333333333")
LAYER_A = LayerId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LAYER_B = LayerId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
LAYER_C = LayerId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SPEC = PixelSpec.colour(PixelFormat.RGBA_U16, PROFILE)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def build_plan(
    *,
    digest: ObjectId = PLAN_A,
    revision: RevisionId = REVISION_A,
    level: int = 1,
) -> ProxyBuildPlan:
    return ProxyBuildPlan(
        digest,
        Rect(0, 0, 16, 8),
        level,
        SPEC,
        revision,
        COMPATIBILITY,
        tile_width=4,
        tile_height=4,
    )


def results_for(plan: ProxyBuildPlan) -> tuple[TileResult, ...]:
    results: list[TileResult] = []
    for index, request in enumerate(plan.requests()):
        byte_count = (
            request.destination.width
            * request.destination.height
            * request.spec.pixel_format.bytes_per_pixel
        )
        payload = bytes((index % 256,)) * byte_count
        results.append(
            TileResult(
                request.source,
                request.destination,
                request.level,
                request.spec,
                request.revision,
                ObjectId.from_bytes(payload),
                index,
                owned_bytes=payload,
            )
        )
    return tuple(results)


class ProxyGeometryAndSelectionTests(unittest.TestCase):
    def test_three_required_levels_use_outward_checked_geometry(self) -> None:
        source = Rect(0, 0, 10_000, 10_000)
        self.assertEqual(PROXY_LEVELS, (1, 2, 3))
        self.assertEqual(
            tuple(proxy_extent(source, level) for level in PROXY_LEVELS),
            (
                Rect(0, 0, 5_000, 5_000),
                Rect(0, 0, 2_500, 2_500),
                Rect(0, 0, 1_250, 1_250),
            ),
        )
        self.assertEqual(proxy_extent(Rect(-1, -1, 4, 4), 1), Rect(-1, -1, 3, 3))

    def test_selection_is_nearest_available_level_not_coarser_than_zoom(self) -> None:
        cases = (
            (2.0, 0),
            (0.5001, 0),
            (0.5, 1),
            (0.2501, 1),
            (0.25, 2),
            (0.1251, 2),
            (0.125, 3),
            (0.001, 3),
        )
        self.assertEqual(tuple(select_proxy_level(value) for value, _ in cases), tuple(
            expected for _, expected in cases
        ))
        for malformed in (0.0, -1.0, float("inf"), float("nan"), True):
            with self.assertRaises(InvalidGraph):
                select_proxy_level(malformed)

    def test_proxy_key_binds_exactly_seven_canonical_components(self) -> None:
        key = ProxyKey(
            PLAN_A,
            1,
            Rect(0, 0, 4, 4),
            PixelFormat.RGBA_U16,
            PROFILE,
            COMPATIBILITY,
        )
        self.assertEqual(len(key.to_data()), 7)
        self.assertEqual(key.algorithm_version, PROXY_ALGORITHM_VERSION)
        self.assertEqual(key.digest, ObjectId.from_bytes(key.canonical_bytes()))


class ProxyPublicationTests(unittest.TestCase):
    def test_build_plan_partitions_exact_bounded_tiles_and_publishes_atomically(self) -> None:
        plan = build_plan()
        requests = plan.requests()
        self.assertEqual(plan.extent, Rect(0, 0, 8, 4))
        self.assertEqual(len(requests), 2)
        self.assertEqual(tuple(item.destination for item in requests), (
            Rect(0, 0, 4, 4),
            Rect(4, 0, 4, 4),
        ))
        self.assertEqual(tuple(item.source for item in requests), (
            Rect(0, 0, 8, 8),
            Rect(8, 0, 8, 8),
        ))
        cache = ProxyCache()
        results = results_for(plan)
        manifest = cache.publish(plan, results, current_revision=REVISION_A)
        self.assertEqual(len(manifest.tiles), 2)
        self.assertEqual(
            sum(item.byte_count for item in manifest.tiles),
            8 * 4 * PixelFormat.RGBA_U16.bytes_per_pixel,
        )
        self.assertIsNotNone(cache.manifest(PLAN_A, 1))
        self.assertTrue(all(cache.tile(item.key) is not None for item in manifest.tiles))

    def test_partial_corrupt_or_stale_build_publishes_zero_manifests(self) -> None:
        plan = build_plan()
        results = results_for(plan)
        cache = ProxyCache()
        with self.assertRaises(InvalidGraph):
            cache.publish(plan, results[:-1], current_revision=REVISION_A)
        self.assertIsNone(cache.manifest(PLAN_A, 1))
        malformed = dataclasses.replace(results[0], destination=Rect(1, 0, 4, 4))
        with self.assertRaises(InvalidGraph):
            cache.publish(plan, (malformed, *results[1:]), current_revision=REVISION_A)
        self.assertIsNone(cache.manifest(PLAN_A, 1))
        with self.assertRaises(CancelledOrStaleWork):
            cache.publish(plan, results, current_revision=REVISION_B)
        self.assertIsNone(cache.manifest(PLAN_A, 1))

    def test_invalidation_removes_only_intersecting_scaled_tiles_and_manifest(self) -> None:
        plan_a = build_plan()
        plan_b = build_plan(digest=PLAN_B)
        cache = ProxyCache()
        manifest_a = cache.publish(
            plan_a,
            results_for(plan_a),
            current_revision=REVISION_A,
        )
        manifest_b = cache.publish(
            plan_b,
            results_for(plan_b),
            current_revision=REVISION_A,
        )
        removed = cache.invalidate((PLAN_A,), (Rect(0, 0, 2, 2),))
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].rectangle, Rect(0, 0, 4, 4))
        self.assertIsNone(cache.manifest(PLAN_A, 1))
        self.assertIsNotNone(cache.manifest(PLAN_B, 1))
        self.assertIsNotNone(cache.tile(manifest_b.tiles[0].key))
        self.assertIsNone(cache.tile(manifest_a.tiles[1].key))


class GraphInvalidationTests(unittest.TestCase):
    def test_changed_descendant_selects_every_dependent_graph_not_unrelated_graphs(self) -> None:
        dependencies = (
            GraphDependency(
                PLAN_A,
                REVISION_A,
                (LAYER_A, LAYER_B),
                (OBJECT_A,),
                Rect(0, 0, 16, 8),
            ),
            GraphDependency(
                PLAN_B,
                REVISION_A,
                (LAYER_C,),
                (),
                Rect(0, 0, 16, 8),
            ),
            GraphDependency(
                ObjectId("6" * 64),
                REVISION_C,
                (LAYER_B,),
                (),
                Rect(0, 0, 16, 8),
            ),
        )
        request = InvalidationRequest(
            REVISION_A,
            REVISION_B,
            (LAYER_B,),
            (),
            (Rect(0, 0, 2, 2),),
        )
        result = plan_invalidation(dependencies, request)
        self.assertEqual(result.graph_digests, (PLAN_A,))
        self.assertEqual(result.affected_rectangles, request.affected_rectangles)

    def test_dependency_inputs_are_sorted_unique_and_revision_advancing(self) -> None:
        with self.assertRaises(InvalidGraph):
            GraphDependency(
                PLAN_A,
                REVISION_A,
                (LAYER_B, LAYER_A),
                (),
                Rect(0, 0, 1, 1),
            )
        with self.assertRaises(InvalidGraph):
            InvalidationRequest(
                REVISION_A,
                REVISION_A,
                (LAYER_A,),
                (),
                (Rect(0, 0, 1, 1),),
            )


class RenderDependencyBoundaryTests(unittest.TestCase):
    def test_proxy_and_invalidation_modules_import_zero_native_runtime_modules(self) -> None:
        for relative in (
            "src/kilix_image_shop/render/graph.py",
            "src/kilix_image_shop/render/proxy.py",
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
                any(
                    name == "gi"
                    or name.startswith("gi.")
                    or name == "kilix_image_shop.engine.runtime"
                    for name in imports
                ),
                relative,
            )


if __name__ == "__main__":
    unittest.main()
