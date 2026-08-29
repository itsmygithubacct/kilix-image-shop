"""Tile-bounded pixel and source-alpha-independent mask stroke plans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy
from kilix_image_shop.domain.commands import ImportAsset
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, RevisionId
from kilix_image_shop.domain.layers import PixelLayer
from kilix_image_shop.engine.api import MASK_TILE_SIZE


class PaintValidationError(ValueError):
    """A paint stroke or output exceeds its finite work contract."""


class StrokeTarget(StrEnum):
    PIXELS = "pixels"
    MASK = "mask"


def _u16(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise PaintValidationError(f"{field} must be an integer in [0, 65535]")
    return value


@dataclass(frozen=True, slots=True)
class PaintLimits:
    max_points: int
    max_tiles: int
    max_brush_diameter: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_points, bool)
            or not isinstance(self.max_points, int)
            or self.max_points <= 0
            or isinstance(self.max_tiles, bool)
            or not isinstance(self.max_tiles, int)
            or self.max_tiles <= 0
            or isinstance(self.max_brush_diameter, bool)
            or not isinstance(self.max_brush_diameter, (int, float))
            or not math.isfinite(float(self.max_brush_diameter))
            or self.max_brush_diameter <= 0
        ):
            raise PaintValidationError("paint limits must be finite and positive")


@dataclass(frozen=True, slots=True)
class Brush:
    target: StrokeTarget
    diameter: float
    opacity_u16: int
    hardness_u16: int
    rgba_u16: tuple[int, int, int, int] | None = None
    mask_value_u8: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, StrokeTarget):
            raise PaintValidationError("brush target must be closed")
        if (
            isinstance(self.diameter, bool)
            or not isinstance(self.diameter, (int, float))
            or not math.isfinite(float(self.diameter))
            or self.diameter <= 0
        ):
            raise PaintValidationError("brush diameter must be finite and positive")
        _u16(self.opacity_u16, "brush opacity")
        _u16(self.hardness_u16, "brush hardness")
        if self.target is StrokeTarget.PIXELS:
            if (
                not isinstance(self.rgba_u16, tuple)
                or len(self.rgba_u16) != 4
                or any(_u16(item, "brush colour") != item for item in self.rgba_u16)
                or self.mask_value_u8 is not None
            ):
                raise PaintValidationError("pixel brush requires exactly one RGBA u16 colour")
        elif (
            self.rgba_u16 is not None
            or isinstance(self.mask_value_u8, bool)
            or not isinstance(self.mask_value_u8, int)
            or not 0 <= self.mask_value_u8 <= 255
        ):
            raise PaintValidationError("mask brush requires exactly one Y u8 value")


@dataclass(frozen=True, slots=True)
class StrokePoint:
    x: float
    y: float
    pressure_u16: int = 65535

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (self.x, self.y)
        ):
            raise PaintValidationError("stroke coordinates must be finite numbers")
        object.__setattr__(self, "x", float(self.x))
        object.__setattr__(self, "y", float(self.y))
        _u16(self.pressure_u16, "stroke pressure")


@dataclass(frozen=True, slots=True)
class Stroke:
    brush: Brush
    points: tuple[StrokePoint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.brush, Brush):
            raise PaintValidationError("stroke requires a typed brush")
        if not isinstance(self.points, tuple) or not self.points or any(
            not isinstance(item, StrokePoint) for item in self.points
        ):
            raise PaintValidationError("stroke requires immutable typed points")


@dataclass(frozen=True, slots=True)
class PaintPlan:
    stroke: Stroke
    extent: Rect
    affected_bounds: Rect
    tiles: tuple[Rect, ...]
    tile_size: int = MASK_TILE_SIZE

    def __post_init__(self) -> None:
        if not isinstance(self.stroke, Stroke) or not isinstance(self.extent, Rect):
            raise PaintValidationError("paint plan requires typed stroke and extent")
        if not isinstance(self.affected_bounds, Rect) or not self.affected_bounds.is_within(
            self.extent
        ):
            raise PaintValidationError("paint plan affected bounds leave its extent")
        if self.tile_size != MASK_TILE_SIZE:
            raise PaintValidationError("paint plan tile size differs from the frozen grid")
        if not isinstance(self.tiles, tuple) or not self.tiles or any(
            not isinstance(item, Rect)
            or not item.is_within(self.extent)
            or item.width > MASK_TILE_SIZE
            or item.height > MASK_TILE_SIZE
            for item in self.tiles
        ):
            raise PaintValidationError("paint plan tiles are malformed or unbounded")
        keys = tuple((item.y, item.x) for item in self.tiles)
        if keys != tuple(sorted(set(keys))):
            raise PaintValidationError("paint plan tiles must be sorted and unique")

    @property
    def uses_source_alpha(self) -> bool:
        return self.stroke.brush.target is StrokeTarget.PIXELS


def _intersects(left: Rect, right: Rect) -> bool:
    return not (
        left.x + left.width <= right.x
        or right.x + right.width <= left.x
        or left.y + left.height <= right.y
        or right.y + right.height <= left.y
    )


def plan_stroke(stroke: Stroke, extent: Rect, limits: PaintLimits) -> PaintPlan:
    if not isinstance(stroke, Stroke) or not isinstance(extent, Rect) or not isinstance(
        limits, PaintLimits
    ):
        raise PaintValidationError("paint planner inputs are malformed")
    if len(stroke.points) > limits.max_points:
        raise PaintValidationError("stroke exceeds its point budget")
    if stroke.brush.diameter > limits.max_brush_diameter:
        raise PaintValidationError("stroke exceeds its brush-diameter budget")
    if any(
        not (
            extent.x <= point.x < extent.x + extent.width
            and extent.y <= point.y < extent.y + extent.height
        )
        for point in stroke.points
    ):
        raise PaintValidationError("stroke point leaves its checked extent")
    radius = float(stroke.brush.diameter) / 2.0
    left = max(extent.x, math.floor(min(point.x for point in stroke.points) - radius))
    top = max(extent.y, math.floor(min(point.y for point in stroke.points) - radius))
    right = min(
        extent.x + extent.width,
        math.ceil(max(point.x for point in stroke.points) + radius),
    )
    bottom = min(
        extent.y + extent.height,
        math.ceil(max(point.y for point in stroke.points) + radius),
    )
    affected = Rect(left, top, max(1, right - left), max(1, bottom - top))
    rectangles: list[Rect] = []
    for y in range(extent.y, extent.y + extent.height, MASK_TILE_SIZE):
        height = min(MASK_TILE_SIZE, extent.y + extent.height - y)
        for x in range(extent.x, extent.x + extent.width, MASK_TILE_SIZE):
            width = min(MASK_TILE_SIZE, extent.x + extent.width - x)
            rectangle = Rect(x, y, width, height)
            if _intersects(rectangle, affected):
                rectangles.append(rectangle)
    if not rectangles or len(rectangles) > limits.max_tiles:
        raise PaintValidationError("stroke exceeds its affected-tile budget")
    return PaintPlan(stroke, extent, affected, tuple(rectangles))


def pixel_stroke_layer_command(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    output_asset: AssetRef,
    layer_id: LayerId,
    name: str,
    parent_id: LayerId | None,
    index: int,
) -> ImportAsset:
    """Publish completed pixel-stroke output as a new non-destructive layer."""

    if output_asset.import_policy is not ImportPolicy.COPIED:
        raise PaintValidationError("pixel stroke output must be project-owned")
    layer = PixelLayer(
        layer_id=layer_id,
        name=name,
        asset_digest=output_asset.digest,
    )
    return ImportAsset(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        asset=output_asset,
        layer=layer,
        parent_id=parent_id,
        index=index,
    )
