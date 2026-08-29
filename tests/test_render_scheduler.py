from __future__ import annotations

import pathlib
import unittest

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.engine.api import (
    CancelToken,
    CancelledOrStaleWork,
    PixelFormat,
    PixelSpec,
    ResourceExhaustion,
    TileRequest,
    TileResult,
)
from kilix_image_shop.render.scheduler import (
    TileBatch,
    TileScheduler,
    WorkPriority,
    partition_tiles,
)


GRAPH_A = ObjectId("1" * 64)
GRAPH_B = ObjectId("2" * 64)
PROFILE = ObjectId("3" * 64)
REVISION_A = RevisionId("11111111-1111-4111-8111-111111111111")
REVISION_B = RevisionId("22222222-2222-4222-8222-222222222222")
SPEC = PixelSpec.colour(PixelFormat.RGBA_U16, PROFILE)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def request(graph: ObjectId, x: int = 0) -> TileRequest:
    return TileRequest(
        graph,
        Rect(x, 0, 2, 2),
        Rect(x, 0, 2, 2),
        0,
        SPEC,
        REVISION_A,
    )


class RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[TileRequest] = []
        self.after_render = None

    def render_tile(self, tile: TileRequest, *, cancel: CancelToken) -> TileResult:
        cancel.raise_if_cancelled()
        self.calls.append(tile)
        byte_count = (
            tile.destination.width
            * tile.destination.height
            * tile.spec.pixel_format.bytes_per_pixel
        )
        payload = bytes((len(self.calls) % 256,)) * byte_count
        result = TileResult(
            tile.source,
            tile.destination,
            tile.level,
            tile.spec,
            tile.revision,
            ObjectId.from_bytes(payload),
            1,
            owned_bytes=payload,
        )
        if self.after_render is not None:
            self.after_render()
        return result


class PartitionTests(unittest.TestCase):
    def test_large_destination_is_partitioned_with_both_dimension_caps(self) -> None:
        requests = partition_tiles(
            graph_digest=GRAPH_A,
            source=Rect(0, 0, 2_000, 1_100),
            destination=Rect(10, 20, 4_000, 2_200),
            level=1,
            spec=SPEC,
            revision=REVISION_A,
        )
        self.assertEqual(len(requests), 9)
        self.assertTrue(all(item.destination.width <= 1_920 for item in requests))
        self.assertTrue(all(item.destination.height <= 1_080 for item in requests))
        self.assertEqual(
            sum(item.destination.width * item.destination.height for item in requests),
            4_000 * 2_200,
        )
        self.assertEqual(requests[0].source, Rect(0, 0, 960, 540))
        self.assertEqual(requests[-1].source, Rect(1920, 1080, 80, 20))
        self.assertTrue(all(item.level == 1 for item in requests))

    def test_integer_mapping_is_outward_and_does_not_use_float_geometry(self) -> None:
        requests = partition_tiles(
            graph_digest=GRAPH_A,
            source=Rect(0, 0, 7, 5),
            destination=Rect(0, 0, 10, 6),
            level=3,
            spec=SPEC,
            revision=REVISION_A,
            maximum_width=4,
            maximum_height=3,
        )
        self.assertEqual(tuple(item.destination.width for item in requests), (4, 4, 2, 4, 4, 2))
        self.assertTrue(all(item.source.is_within(Rect(0, 0, 7, 5)) for item in requests))


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RecordingEngine()
        self.current = [REVISION_A]
        self.cancel = CancelToken()

    def revision(self) -> RevisionId:
        return self.current[0]

    def test_priority_order_is_interactive_proxy_thumbnail_export(self) -> None:
        scheduler = TileScheduler(8)
        batches = (
            TileBatch((request(GRAPH_A, 6),), WorkPriority.EXPORT),
            TileBatch((request(GRAPH_A, 2),), WorkPriority.PROXY),
            TileBatch((request(GRAPH_A, 0),), WorkPriority.INTERACTIVE),
            TileBatch((request(GRAPH_A, 4),), WorkPriority.THUMBNAIL),
        )
        for batch in batches:
            scheduler.submit(batch)
        completed = tuple(
            scheduler.run_next(
                self.engine,
                current_revision=self.revision,
                cancel=self.cancel,
            )
            for _ in batches
        )
        self.assertEqual(
            tuple(item.priority for item in completed if item is not None),
            (
                WorkPriority.INTERACTIVE,
                WorkPriority.PROXY,
                WorkPriority.THUMBNAIL,
                WorkPriority.EXPORT,
            ),
        )
        self.assertEqual(scheduler.queued_tiles, 0)
        self.assertEqual(scheduler.queued_batches, 0)

    def test_queue_ceiling_and_pending_cancellation_are_exact(self) -> None:
        scheduler = TileScheduler(2)
        batch = TileBatch((request(GRAPH_A), request(GRAPH_A, 2)), WorkPriority.PROXY)
        digest = scheduler.submit(batch)
        with self.assertRaises(ResourceExhaustion):
            scheduler.submit(TileBatch((request(GRAPH_B),), WorkPriority.INTERACTIVE))
        self.assertEqual(scheduler.cancel_pending(digest), 2)
        self.assertEqual(scheduler.cancel_pending(digest), 0)
        self.assertEqual(scheduler.queued_tiles, 0)

    def test_cancel_before_first_tile_calls_engine_zero_times(self) -> None:
        scheduler = TileScheduler(2)
        scheduler.submit(TileBatch((request(GRAPH_A),), WorkPriority.INTERACTIVE))
        self.cancel.cancel()
        with self.assertRaises(CancelledOrStaleWork):
            scheduler.run_next(
                self.engine,
                current_revision=self.revision,
                cancel=self.cancel,
            )
        self.assertEqual(len(self.engine.calls), 0)

    def test_cancel_after_native_tile_publishes_zero_completed_batches(self) -> None:
        scheduler = TileScheduler(2)
        scheduler.submit(
            TileBatch((request(GRAPH_A), request(GRAPH_A, 2)), WorkPriority.INTERACTIVE)
        )
        self.engine.after_render = self.cancel.cancel
        with self.assertRaises(CancelledOrStaleWork):
            scheduler.run_next(
                self.engine,
                current_revision=self.revision,
                cancel=self.cancel,
            )
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(scheduler.queued_batches, 0)

    def test_revision_change_after_native_tile_suppresses_stale_completion(self) -> None:
        scheduler = TileScheduler(2)
        scheduler.submit(TileBatch((request(GRAPH_A),), WorkPriority.INTERACTIVE))
        self.engine.after_render = lambda: self.current.__setitem__(0, REVISION_B)
        with self.assertRaises(CancelledOrStaleWork):
            scheduler.run_next(
                self.engine,
                current_revision=self.revision,
                cancel=self.cancel,
            )
        self.assertEqual(len(self.engine.calls), 1)

    def test_batch_identity_and_result_order_are_deterministic(self) -> None:
        batch = TileBatch(
            (request(GRAPH_A), request(GRAPH_A, 2)),
            WorkPriority.INTERACTIVE,
        )
        scheduler = TileScheduler(2)
        self.assertEqual(scheduler.submit(batch), ObjectId.from_bytes(batch.canonical_bytes()))
        completed = scheduler.run_next(
            self.engine,
            current_revision=self.revision,
            cancel=self.cancel,
        )
        assert completed is not None
        self.assertEqual(tuple(item.destination.x for item in completed.results), (0, 2))
        self.assertEqual(completed.elapsed_ns, 2)


class SchedulerDependencyTests(unittest.TestCase):
    def test_scheduler_contains_zero_thread_pools_and_zero_gegl_processors(self) -> None:
        source = (ROOT / "src/kilix_image_shop/render/scheduler.py").read_text()
        self.assertNotIn("ThreadPool" + "Executor", source)
        self.assertNotIn("new_" + "processor", source)
        self.assertNotIn("gi.repository", source)


if __name__ == "__main__":
    unittest.main()
