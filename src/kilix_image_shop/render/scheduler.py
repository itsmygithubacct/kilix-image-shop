"""Bounded, priority-ordered tile scheduling with revision publication gates."""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.engine.api import (
    CancelToken,
    CancelledOrStaleWork,
    ImageEngine,
    InvalidGraph,
    PixelSpec,
    ResourceExhaustion,
    TileRequest,
    TileResult,
)


class WorkPriority(StrEnum):
    INTERACTIVE = "interactive"
    PROXY = "proxy"
    THUMBNAIL = "thumbnail"
    EXPORT = "export"

    @property
    def rank(self) -> int:
        return {
            WorkPriority.INTERACTIVE: 0,
            WorkPriority.PROXY: 1,
            WorkPriority.THUMBNAIL: 2,
            WorkPriority.EXPORT: 3,
        }[self]


@dataclass(frozen=True, slots=True)
class TileBatch:
    requests: tuple[TileRequest, ...]
    priority: WorkPriority

    def __post_init__(self) -> None:
        if not isinstance(self.requests, tuple) or not self.requests or any(
            not isinstance(item, TileRequest) for item in self.requests
        ):
            raise InvalidGraph("tile batch requires non-empty immutable requests")
        if not isinstance(self.priority, WorkPriority):
            raise InvalidGraph("tile batch priority is outside the closed queue")
        identity = (
            self.requests[0].graph_digest,
            self.requests[0].level,
            self.requests[0].spec,
            self.requests[0].revision,
        )
        if any(
            (
                item.graph_digest,
                item.level,
                item.spec,
                item.revision,
            )
            != identity
            for item in self.requests
        ):
            raise InvalidGraph("tile batch requests do not share one render identity")
        destinations = tuple(
            (item.destination.y, item.destination.x, item.destination.height, item.destination.width)
            for item in self.requests
        )
        if destinations != tuple(sorted(set(destinations))):
            raise InvalidGraph("tile batch destinations must be sorted and unique")

    @property
    def revision(self) -> RevisionId:
        return self.requests[0].revision

    def canonical_bytes(self) -> bytes:
        value = {
            "priority": self.priority.value,
            "requests": [item.to_data() for item in self.requests],
        }
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class CompletedBatch:
    batch_digest: ObjectId
    priority: WorkPriority
    revision: RevisionId
    results: tuple[TileResult, ...]
    elapsed_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.batch_digest, ObjectId) or not isinstance(
            self.priority, WorkPriority
        ):
            raise InvalidGraph("completed batch identity is malformed")
        if not isinstance(self.revision, RevisionId):
            raise InvalidGraph("completed batch revision is malformed")
        if not isinstance(self.results, tuple) or not self.results or any(
            not isinstance(item, TileResult) or item.revision != self.revision
            for item in self.results
        ):
            raise InvalidGraph("completed batch results are malformed")
        if (
            isinstance(self.elapsed_ns, bool)
            or not isinstance(self.elapsed_ns, int)
            or self.elapsed_ns < 0
        ):
            raise InvalidGraph("completed batch timing is malformed")


def _mapped_source(
    source: Rect,
    destination: Rect,
    tile: Rect,
) -> Rect:
    relative_left = tile.x - destination.x
    relative_top = tile.y - destination.y
    relative_right = relative_left + tile.width
    relative_bottom = relative_top + tile.height
    left = source.x + (relative_left * source.width) // destination.width
    top = source.y + (relative_top * source.height) // destination.height
    right = source.x - (-(relative_right * source.width) // destination.width)
    bottom = source.y - (-(relative_bottom * source.height) // destination.height)
    return Rect(left, top, right - left, bottom - top)


def partition_tiles(
    *,
    graph_digest: ObjectId,
    source: Rect,
    destination: Rect,
    level: int,
    spec: PixelSpec,
    revision: RevisionId,
    maximum_width: int = TileRequest.MAX_WIDTH,
    maximum_height: int = TileRequest.MAX_HEIGHT,
) -> tuple[TileRequest, ...]:
    if not isinstance(graph_digest, ObjectId) or not isinstance(source, Rect) or not isinstance(
        destination, Rect
    ):
        raise InvalidGraph("tile partition requires graph identity and checked geometry")
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 3:
        raise InvalidGraph("tile partition level must be in [0, 3]")
    if not isinstance(spec, PixelSpec) or not isinstance(revision, RevisionId):
        raise InvalidGraph("tile partition requires pixel and revision identity")
    for value, maximum, label in (
        (maximum_width, TileRequest.MAX_WIDTH, "tile width"),
        (maximum_height, TileRequest.MAX_HEIGHT, "tile height"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 < value <= maximum
        ):
            raise InvalidGraph(f"{label} is outside the engine envelope")
    requests: list[TileRequest] = []
    bottom = destination.y + destination.height
    right = destination.x + destination.width
    for y in range(destination.y, bottom, maximum_height):
        height = min(maximum_height, bottom - y)
        for x in range(destination.x, right, maximum_width):
            width = min(maximum_width, right - x)
            tile = Rect(x, y, width, height)
            requests.append(
                TileRequest(
                    graph_digest,
                    _mapped_source(source, destination, tile),
                    tile,
                    level,
                    spec,
                    revision,
                )
            )
    return tuple(requests)


class TileScheduler:
    """A synchronous owner-executor queue; it creates no thread pool."""

    def __init__(self, max_queued_tiles: int) -> None:
        if (
            isinstance(max_queued_tiles, bool)
            or not isinstance(max_queued_tiles, int)
            or max_queued_tiles <= 0
        ):
            raise InvalidGraph("tile queue requires a finite positive tile ceiling")
        self._maximum = max_queued_tiles
        self._queued_tiles = 0
        self._sequence = 0
        self._queue: list[tuple[int, int, TileBatch]] = []
        self._digests: set[ObjectId] = set()

    @property
    def queued_tiles(self) -> int:
        return self._queued_tiles

    @property
    def queued_batches(self) -> int:
        return len(self._queue)

    def submit(self, batch: TileBatch) -> ObjectId:
        if not isinstance(batch, TileBatch):
            raise InvalidGraph("scheduler submission requires a typed tile batch")
        wanted = self._queued_tiles + len(batch.requests)
        if wanted > self._maximum:
            raise ResourceExhaustion("tile queue would exceed its explicit ceiling")
        digest = batch.digest
        if digest in self._digests:
            raise InvalidGraph("tile batch is already queued")
        heapq.heappush(
            self._queue,
            (batch.priority.rank, self._sequence, batch),
        )
        self._sequence += 1
        self._queued_tiles = wanted
        self._digests.add(digest)
        return digest

    def cancel_pending(self, batch_digest: ObjectId) -> int:
        if not isinstance(batch_digest, ObjectId):
            raise InvalidGraph("pending cancellation requires a batch digest")
        retained: list[tuple[int, int, TileBatch]] = []
        removed = 0
        for entry in self._queue:
            if entry[2].digest == batch_digest:
                removed += len(entry[2].requests)
                self._digests.discard(batch_digest)
            else:
                retained.append(entry)
        if removed:
            self._queue = retained
            heapq.heapify(self._queue)
            self._queued_tiles -= removed
        return removed

    @staticmethod
    def _require_current(
        expected: RevisionId,
        current_revision: Callable[[], RevisionId],
    ) -> None:
        observed = current_revision()
        if not isinstance(observed, RevisionId):
            raise InvalidGraph("current revision provider returned an untyped value")
        if observed != expected:
            raise CancelledOrStaleWork("tile batch revision is no longer current")

    def run_next(
        self,
        engine: ImageEngine,
        *,
        current_revision: Callable[[], RevisionId],
        cancel: CancelToken,
    ) -> CompletedBatch | None:
        if not self._queue:
            return None
        if not callable(current_revision) or not isinstance(cancel, CancelToken):
            raise InvalidGraph("scheduler run requires revision and cancellation ports")
        _, _, batch = heapq.heappop(self._queue)
        self._queued_tiles -= len(batch.requests)
        self._digests.remove(batch.digest)
        cancel.raise_if_cancelled()
        self._require_current(batch.revision, current_revision)
        results: list[TileResult] = []
        for request in batch.requests:
            cancel.raise_if_cancelled()
            self._require_current(batch.revision, current_revision)
            result = engine.render_tile(request, cancel=cancel)
            cancel.raise_if_cancelled()
            self._require_current(batch.revision, current_revision)
            if result.revision != batch.revision:
                raise CancelledOrStaleWork("engine returned a stale tile revision")
            cancel.raise_if_cancelled()
            results.append(result)
        cancel.raise_if_cancelled()
        self._require_current(batch.revision, current_revision)
        completed = CompletedBatch(
            batch.digest,
            batch.priority,
            batch.revision,
            tuple(results),
            sum(item.elapsed_ns for item in results),
        )
        cancel.raise_if_cancelled()
        self._require_current(batch.revision, current_revision)
        return completed


__all__ = (
    "CompletedBatch",
    "TileBatch",
    "TileScheduler",
    "WorkPriority",
    "partition_tiles",
)
