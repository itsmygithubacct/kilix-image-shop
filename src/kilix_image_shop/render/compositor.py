"""Viewport and proxy composition over the bounded owner-executor scheduler."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.engine.api import (
    CancelToken,
    CancelledOrStaleWork,
    ImageEngine,
    InvalidGraph,
    PixelSpec,
    TileResult,
)

from .plan import RenderPlan
from .proxy import PROXY_LEVELS, ProxyBuildPlan, ProxyCache, select_proxy_level
from .scheduler import TileBatch, TileScheduler, WorkPriority, partition_tiles


CurrentRevision = Callable[[], RevisionId]


def _scaled_outward(rectangle: Rect, level: int) -> Rect:
    denominator = 1 << level
    left = rectangle.x // denominator
    top = rectangle.y // denominator
    right = -(-(rectangle.x + rectangle.width) // denominator)
    bottom = -(-(rectangle.y + rectangle.height) // denominator)
    return Rect(left, top, right - left, bottom - top)


@dataclass(frozen=True, slots=True)
class CompositionRequest:
    source: Rect
    destination: Rect
    zoom_ratio: float
    priority: WorkPriority = WorkPriority.INTERACTIVE

    def __post_init__(self) -> None:
        if not isinstance(self.source, Rect) or not isinstance(self.destination, Rect):
            raise InvalidGraph("composition request requires checked geometry")
        if (
            isinstance(self.zoom_ratio, bool)
            or not isinstance(self.zoom_ratio, (int, float))
            or not math.isfinite(float(self.zoom_ratio))
            or self.zoom_ratio <= 0
        ):
            raise InvalidGraph("composition zoom ratio must be finite and positive")
        if not isinstance(self.priority, WorkPriority) or self.priority is WorkPriority.EXPORT:
            raise InvalidGraph("viewport composition priority is outside its closed set")


@dataclass(frozen=True, slots=True)
class CompositedImage:
    plan_digest: ObjectId
    revision: RevisionId
    source: Rect
    destination: Rect
    level: int
    spec: PixelSpec
    payload: bytes
    payload_digest: ObjectId
    tile_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.plan_digest, ObjectId) or not isinstance(
            self.revision, RevisionId
        ):
            raise InvalidGraph("composited image identity is malformed")
        if not isinstance(self.source, Rect) or not isinstance(self.destination, Rect):
            raise InvalidGraph("composited image geometry is malformed")
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 3
        ):
            raise InvalidGraph("composited image level is malformed")
        if not isinstance(self.spec, PixelSpec) or not isinstance(self.payload, bytes):
            raise InvalidGraph("composited image pixels are malformed")
        expected = (
            self.destination.width
            * self.destination.height
            * self.spec.pixel_format.bytes_per_pixel
        )
        if (
            len(self.payload) != expected
            or ObjectId.from_bytes(self.payload) != self.payload_digest
        ):
            raise InvalidGraph("composited image payload differs from its geometry or digest")
        if (
            isinstance(self.tile_count, bool)
            or not isinstance(self.tile_count, int)
            or self.tile_count <= 0
        ):
            raise InvalidGraph("composited image tile count must be positive")


def assemble_tiles(
    results: tuple[TileResult, ...],
    destination: Rect,
    spec: PixelSpec,
    *,
    cancel: CancelToken,
) -> bytes:
    """Assemble exact non-overlapping tiles, checking cancellation before every row."""

    if not isinstance(results, tuple) or not results or any(
        not isinstance(item, TileResult) for item in results
    ):
        raise InvalidGraph("tile assembly requires immutable typed results")
    if not isinstance(destination, Rect) or not isinstance(spec, PixelSpec):
        raise InvalidGraph("tile assembly requires destination and pixel identity")
    bytes_per_pixel = spec.pixel_format.bytes_per_pixel
    output = bytearray(destination.width * destination.height * bytes_per_pixel)
    occupied: set[tuple[int, int]] = set()
    for result in results:
        if result.spec != spec or result.owned_bytes is None or not result.destination.is_within(
            destination
        ):
            raise InvalidGraph("tile result differs from assembly identity")
        tile = result.destination
        for y in range(tile.height):
            cancel.raise_if_cancelled()
            destination_y = tile.y - destination.y + y
            source_start = y * tile.width * bytes_per_pixel
            source_end = source_start + tile.width * bytes_per_pixel
            output_start = (
                destination_y * destination.width + tile.x - destination.x
            ) * bytes_per_pixel
            output_end = output_start + tile.width * bytes_per_pixel
            output[output_start:output_end] = result.owned_bytes[source_start:source_end]
        for y in range(tile.y, tile.y + tile.height):
            for x in range(tile.x, tile.x + tile.width):
                coordinate = (x, y)
                if coordinate in occupied:
                    raise InvalidGraph("tile results overlap during assembly")
                occupied.add(coordinate)
    if len(occupied) != destination.width * destination.height:
        raise InvalidGraph("tile results do not completely cover the destination")
    return bytes(output)


class Compositor:
    """Coordinate complete proxy publication and one atomic viewport result."""

    def __init__(
        self,
        engine: ImageEngine,
        scheduler: TileScheduler,
        proxies: ProxyCache,
    ) -> None:
        if not isinstance(scheduler, TileScheduler) or not isinstance(proxies, ProxyCache):
            raise InvalidGraph("compositor requires typed scheduler and proxy cache")
        self._engine = engine
        self._scheduler = scheduler
        self._proxies = proxies

    @staticmethod
    def _require_current(plan: RenderPlan, current_revision: CurrentRevision) -> None:
        observed = current_revision()
        if not isinstance(observed, RevisionId):
            raise InvalidGraph("current revision provider returned an untyped value")
        if observed != plan.revision:
            raise CancelledOrStaleWork("render plan revision is no longer current")

    def compile(
        self,
        plan: RenderPlan,
        *,
        current_revision: CurrentRevision,
        cancel: CancelToken,
    ) -> ObjectId:
        cancel.raise_if_cancelled()
        self._require_current(plan, current_revision)
        digest = self._engine.compile_graph(plan.graph, cancel=cancel)
        cancel.raise_if_cancelled()
        self._require_current(plan, current_revision)
        if digest != plan.digest:
            raise InvalidGraph("engine compiled a different render-plan identity")
        return digest

    def build_proxies(
        self,
        plan: RenderPlan,
        *,
        current_revision: CurrentRevision,
        cancel: CancelToken,
    ) -> tuple[int, ...]:
        self.compile(plan, current_revision=current_revision, cancel=cancel)
        published: list[int] = []
        for level in PROXY_LEVELS:
            cancel.raise_if_cancelled()
            self._require_current(plan, current_revision)
            build = ProxyBuildPlan(
                plan.digest,
                plan.output_bounds,
                level,
                plan.output_spec,
                plan.revision,
                plan.compatibility_digest,
            )
            results = self._engine.build_proxy(build.requests(), cancel=cancel)
            cancel.raise_if_cancelled()
            self._require_current(plan, current_revision)
            self._proxies.publish(
                build,
                results,
                current_revision=plan.revision,
            )
            published.append(level)
        return tuple(published)

    def compose(
        self,
        plan: RenderPlan,
        request: CompositionRequest,
        *,
        current_revision: CurrentRevision,
        cancel: CancelToken,
    ) -> CompositedImage:
        # Checkpoint 1/6: before graph resolution.
        cancel.raise_if_cancelled()
        if not isinstance(plan, RenderPlan) or not isinstance(request, CompositionRequest):
            raise InvalidGraph("composition requires typed plan and request")
        if not request.source.is_within(plan.output_bounds):
            raise InvalidGraph("composition source leaves the render-plan output")
        if self._scheduler.queued_batches:
            raise InvalidGraph("synchronous compositor requires an idle owner queue")
        self.compile(plan, current_revision=current_revision, cancel=cancel)
        level = select_proxy_level(request.zoom_ratio)
        if level > 0 and self._proxies.manifest(plan.digest, level) is None:
            raise InvalidGraph("selected proxy level has not been completely published")
        source = request.source if level == 0 else _scaled_outward(request.source, level)
        requests = partition_tiles(
            graph_digest=plan.digest,
            source=source,
            destination=request.destination,
            level=level,
            spec=plan.output_spec,
            revision=plan.revision,
        )
        batch = TileBatch(requests, request.priority)
        self._scheduler.submit(batch)
        completed = self._scheduler.run_next(
            self._engine,
            current_revision=current_revision,
            cancel=cancel,
        )
        if completed is None or completed.batch_digest != batch.digest:
            raise InvalidGraph("owner scheduler completed a different viewport batch")
        # Checkpoint 4/6 occurs inside assembly before every output row.
        payload = assemble_tiles(
            completed.results,
            request.destination,
            plan.output_spec,
            cancel=cancel,
        )
        # Checkpoint 5/6: before publishing the completed viewport value.
        cancel.raise_if_cancelled()
        self._require_current(plan, current_revision)
        return CompositedImage(
            plan.digest,
            plan.revision,
            source,
            request.destination,
            level,
            plan.output_spec,
            payload,
            ObjectId.from_bytes(payload),
            len(completed.results),
        )


__all__ = (
    "CompositedImage",
    "CompositionRequest",
    "Compositor",
    "assemble_tiles",
)
