"""Proxy pyramid keys, complete manifests, selection, and invalidation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.engine.api import (
    CancelledOrStaleWork,
    InvalidGraph,
    PixelFormat,
    PixelSpec,
    TileRequest,
    TileResult,
)


PROXY_LEVELS: tuple[int, ...] = (1, 2, 3)
PROXY_ALGORITHM_VERSION = "kilix.proxy-pyramid/v1"
_ALGORITHM_RE = re.compile(r"[a-z][a-z0-9.-]{0,63}/v[1-9][0-9]*\Z")


def _level(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in PROXY_LEVELS:
        raise InvalidGraph("proxy level must be one of the three admitted levels")
    return value


def _algorithm(value: object) -> str:
    if not isinstance(value, str) or _ALGORITHM_RE.fullmatch(value) is None:
        raise InvalidGraph("proxy algorithm version is not canonical")
    return value


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)


def proxy_extent(source: Rect, level: int) -> Rect:
    if not isinstance(source, Rect):
        raise InvalidGraph("proxy source extent must be checked geometry")
    denominator = 1 << _level(level)
    left = source.x // denominator
    top = source.y // denominator
    right = _ceil_div(source.x + source.width, denominator)
    bottom = _ceil_div(source.y + source.height, denominator)
    return Rect(left, top, right - left, bottom - top)


def _rectangle_key(rectangle: Rect) -> tuple[int, int, int, int]:
    return (rectangle.y, rectangle.x, rectangle.height, rectangle.width)


def _intersects(left: Rect, right: Rect) -> bool:
    return not (
        left.x + left.width <= right.x
        or right.x + right.width <= left.x
        or left.y + left.height <= right.y
        or right.y + right.height <= left.y
    )


@dataclass(frozen=True, slots=True)
class ProxyKey:
    """The frozen 7/7-component identity of one regenerable proxy tile."""

    plan_digest: ObjectId
    level: int
    rectangle: Rect
    working_format: PixelFormat
    profile_digest: ObjectId
    compatibility_digest: ObjectId
    algorithm_version: str = PROXY_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.plan_digest, ObjectId):
            raise InvalidGraph("proxy key requires a render-plan digest")
        _level(self.level)
        if not isinstance(self.rectangle, Rect):
            raise InvalidGraph("proxy key requires checked tile geometry")
        if not isinstance(self.working_format, PixelFormat) or self.working_format not in {
            PixelFormat.RGBA_U16,
            PixelFormat.RGBA_FLOAT,
        }:
            raise InvalidGraph("proxy tiles must use a profiled RGBA working format")
        if not isinstance(self.profile_digest, ObjectId) or not isinstance(
            self.compatibility_digest, ObjectId
        ):
            raise InvalidGraph("proxy key requires profile and compatibility digests")
        _algorithm(self.algorithm_version)

    def to_data(self) -> dict[str, object]:
        return {
            "algorithmVersion": self.algorithm_version,
            "compatibilitySha256": self.compatibility_digest.value,
            "level": self.level,
            "planSha256": self.plan_digest.value,
            "profileSha256": self.profile_digest.value,
            "rectangle": self.rectangle.to_data(),
            "workingFormat": self.working_format.value,
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.to_data(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class ProxyTile:
    key: ProxyKey
    payload_digest: ObjectId
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, ProxyKey) or not isinstance(
            self.payload_digest, ObjectId
        ):
            raise InvalidGraph("proxy tile requires typed key and payload identity")
        expected = (
            self.key.rectangle.width
            * self.key.rectangle.height
            * self.key.working_format.bytes_per_pixel
        )
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count != expected
        ):
            raise InvalidGraph("proxy tile byte count differs from its geometry")

    def to_data(self) -> dict[str, object]:
        return {
            "key": self.key.to_data(),
            "payloadSha256": self.payload_digest.value,
            "byteCount": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class ProxyManifest:
    plan_digest: ObjectId
    level: int
    extent: Rect
    spec: PixelSpec
    compatibility_digest: ObjectId
    tiles: tuple[ProxyTile, ...]
    algorithm_version: str = PROXY_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.plan_digest, ObjectId):
            raise InvalidGraph("proxy manifest requires a render-plan digest")
        _level(self.level)
        if not isinstance(self.extent, Rect) or not isinstance(self.spec, PixelSpec):
            raise InvalidGraph("proxy manifest extent or pixel spec is malformed")
        if self.spec.pixel_format not in {PixelFormat.RGBA_U16, PixelFormat.RGBA_FLOAT}:
            raise InvalidGraph("proxy manifest must contain profiled RGBA pixels")
        if not isinstance(self.spec.profile_digest, ObjectId) or not isinstance(
            self.compatibility_digest, ObjectId
        ):
            raise InvalidGraph("proxy manifest lacks profile or compatibility identity")
        _algorithm(self.algorithm_version)
        if not isinstance(self.tiles, tuple) or not self.tiles or any(
            not isinstance(item, ProxyTile) for item in self.tiles
        ):
            raise InvalidGraph("proxy manifest requires immutable typed tiles")
        keys = tuple(_rectangle_key(item.key.rectangle) for item in self.tiles)
        if keys != tuple(sorted(set(keys))):
            raise InvalidGraph("proxy manifest tiles must be sorted and unique")
        area = 0
        for index, tile in enumerate(self.tiles):
            key = tile.key
            if (
                key.plan_digest != self.plan_digest
                or key.level != self.level
                or key.working_format is not self.spec.pixel_format
                or key.profile_digest != self.spec.profile_digest
                or key.compatibility_digest != self.compatibility_digest
                or key.algorithm_version != self.algorithm_version
                or not key.rectangle.is_within(self.extent)
            ):
                raise InvalidGraph("proxy tile identity differs from its manifest")
            if any(
                _intersects(key.rectangle, previous.key.rectangle)
                for previous in self.tiles[:index]
            ):
                raise InvalidGraph("proxy manifest tiles overlap")
            area += key.rectangle.width * key.rectangle.height
        if area != self.extent.width * self.extent.height:
            raise InvalidGraph("proxy manifest is not a complete level covering")

    def to_data(self) -> dict[str, object]:
        return {
            "algorithmVersion": self.algorithm_version,
            "compatibilitySha256": self.compatibility_digest.value,
            "extent": self.extent.to_data(),
            "level": self.level,
            "planSha256": self.plan_digest.value,
            "spec": self.spec.to_data(),
            "tiles": [item.to_data() for item in self.tiles],
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.to_data(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class ProxyBuildPlan:
    plan_digest: ObjectId
    source_extent: Rect
    level: int
    spec: PixelSpec
    revision: RevisionId
    compatibility_digest: ObjectId
    algorithm_version: str = PROXY_ALGORITHM_VERSION
    tile_width: int = TileRequest.MAX_WIDTH
    tile_height: int = TileRequest.MAX_HEIGHT

    def __post_init__(self) -> None:
        if not isinstance(self.plan_digest, ObjectId) or not isinstance(
            self.source_extent, Rect
        ):
            raise InvalidGraph("proxy build plan requires plan identity and source extent")
        _level(self.level)
        if not isinstance(self.spec, PixelSpec) or self.spec.pixel_format not in {
            PixelFormat.RGBA_U16,
            PixelFormat.RGBA_FLOAT,
        }:
            raise InvalidGraph("proxy build plan requires a profiled RGBA pixel spec")
        if not isinstance(self.revision, RevisionId) or not isinstance(
            self.compatibility_digest, ObjectId
        ):
            raise InvalidGraph("proxy build plan requires revision and compatibility")
        _algorithm(self.algorithm_version)
        for value, maximum, label in (
            (self.tile_width, TileRequest.MAX_WIDTH, "proxy tile width"),
            (self.tile_height, TileRequest.MAX_HEIGHT, "proxy tile height"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 < value <= maximum
            ):
                raise InvalidGraph(f"{label} is outside the bounded envelope")

    @property
    def extent(self) -> Rect:
        return proxy_extent(self.source_extent, self.level)

    def requests(self) -> tuple[TileRequest, ...]:
        denominator = 1 << self.level
        destination_extent = self.extent
        requests: list[TileRequest] = []
        bottom = destination_extent.y + destination_extent.height
        right = destination_extent.x + destination_extent.width
        for y in range(destination_extent.y, bottom, self.tile_height):
            height = min(self.tile_height, bottom - y)
            for x in range(destination_extent.x, right, self.tile_width):
                width = min(self.tile_width, right - x)
                destination = Rect(x, y, width, height)
                source_left = max(self.source_extent.x, x * denominator)
                source_top = max(self.source_extent.y, y * denominator)
                source_right = min(
                    self.source_extent.x + self.source_extent.width,
                    (x + width) * denominator,
                )
                source_bottom = min(
                    self.source_extent.y + self.source_extent.height,
                    (y + height) * denominator,
                )
                requests.append(
                    TileRequest(
                        self.plan_digest,
                        Rect(
                            source_left,
                            source_top,
                            source_right - source_left,
                            source_bottom - source_top,
                        ),
                        destination,
                        self.level,
                        self.spec,
                        self.revision,
                    )
                )
        return tuple(requests)

    def manifest(self, results: tuple[TileResult, ...]) -> ProxyManifest:
        requests = self.requests()
        if not isinstance(results, tuple) or len(results) != len(requests):
            raise InvalidGraph("proxy results do not cover the complete build plan")
        tiles: list[ProxyTile] = []
        profile = self.spec.profile_digest
        assert isinstance(profile, ObjectId)
        for request, result in zip(requests, results, strict=True):
            if not isinstance(result, TileResult) or (
                result.source != request.source
                or result.destination != request.destination
                or result.level != request.level
                or result.spec != request.spec
                or result.revision != request.revision
                or result.owned_bytes is None
            ):
                raise InvalidGraph("proxy result differs from its exact tile request")
            key = ProxyKey(
                self.plan_digest,
                self.level,
                result.destination,
                self.spec.pixel_format,
                profile,
                self.compatibility_digest,
                self.algorithm_version,
            )
            tiles.append(ProxyTile(key, result.payload_digest, len(result.owned_bytes)))
        return ProxyManifest(
            self.plan_digest,
            self.level,
            self.extent,
            self.spec,
            self.compatibility_digest,
            tuple(tiles),
            self.algorithm_version,
        )


def select_proxy_level(zoom_ratio: float) -> int:
    if (
        isinstance(zoom_ratio, bool)
        or not isinstance(zoom_ratio, (int, float))
        or not math.isfinite(float(zoom_ratio))
        or zoom_ratio <= 0
    ):
        raise InvalidGraph("zoom ratio must be a finite positive number")
    ratio = float(zoom_ratio)
    if ratio > 0.5:
        return 0
    if ratio > 0.25:
        return 1
    if ratio > 0.125:
        return 2
    return 3


def _scaled_outward(rectangle: Rect, level: int) -> Rect:
    denominator = 1 << level
    left = rectangle.x // denominator
    top = rectangle.y // denominator
    right = _ceil_div(rectangle.x + rectangle.width, denominator)
    bottom = _ceil_div(rectangle.y + rectangle.height, denominator)
    return Rect(left, top, right - left, bottom - top)


class ProxyCache:
    """Publishes only complete verified levels and invalidates precise tiles."""

    def __init__(self) -> None:
        self._tiles: dict[ProxyKey, TileResult] = {}
        self._manifests: dict[tuple[ObjectId, int], ProxyManifest] = {}

    def publish(
        self,
        plan: ProxyBuildPlan,
        results: tuple[TileResult, ...],
        *,
        current_revision: RevisionId,
    ) -> ProxyManifest:
        if not isinstance(plan, ProxyBuildPlan) or not isinstance(
            current_revision, RevisionId
        ):
            raise InvalidGraph("proxy publication requires typed plan and revision")
        if plan.revision != current_revision:
            raise CancelledOrStaleWork("stale proxy build cannot update the current cache")
        manifest = plan.manifest(results)
        additions = dict(zip((item.key for item in manifest.tiles), results, strict=True))
        self._tiles.update(additions)
        self._manifests[(manifest.plan_digest, manifest.level)] = manifest
        return manifest

    def manifest(self, plan_digest: ObjectId, level: int) -> ProxyManifest | None:
        if not isinstance(plan_digest, ObjectId):
            raise InvalidGraph("proxy lookup requires a plan digest")
        _level(level)
        return self._manifests.get((plan_digest, level))

    def tile(self, key: ProxyKey) -> TileResult | None:
        if not isinstance(key, ProxyKey):
            raise InvalidGraph("proxy tile lookup requires a typed key")
        manifest = self._manifests.get((key.plan_digest, key.level))
        if manifest is None or key not in {item.key for item in manifest.tiles}:
            return None
        return self._tiles.get(key)

    def invalidate(
        self,
        plan_digests: tuple[ObjectId, ...],
        affected_rectangles: tuple[Rect, ...],
    ) -> tuple[ProxyKey, ...]:
        if not isinstance(plan_digests, tuple) or any(
            not isinstance(item, ObjectId) for item in plan_digests
        ):
            raise InvalidGraph("proxy invalidation requires typed plan digests")
        identities = tuple(item.value for item in plan_digests)
        if identities != tuple(sorted(set(identities))):
            raise InvalidGraph("proxy invalidation plan digests must be sorted and unique")
        if not isinstance(affected_rectangles, tuple) or not affected_rectangles or any(
            not isinstance(item, Rect) for item in affected_rectangles
        ):
            raise InvalidGraph("proxy invalidation requires affected rectangles")
        wanted = set(plan_digests)
        removed: list[ProxyKey] = []
        for key in tuple(self._tiles):
            if key.plan_digest not in wanted:
                continue
            scaled = tuple(
                _scaled_outward(item, key.level) for item in affected_rectangles
            )
            if any(_intersects(key.rectangle, item) for item in scaled):
                removed.append(key)
                del self._tiles[key]
        for key in removed:
            self._manifests.pop((key.plan_digest, key.level), None)
        return tuple(
            sorted(
                removed,
                key=lambda item: (
                    item.plan_digest.value,
                    item.level,
                    *_rectangle_key(item.rectangle),
                ),
            )
        )


__all__ = (
    "PROXY_ALGORITHM_VERSION",
    "PROXY_LEVELS",
    "ProxyBuildPlan",
    "ProxyCache",
    "ProxyKey",
    "ProxyManifest",
    "ProxyTile",
    "proxy_extent",
    "select_proxy_level",
)
