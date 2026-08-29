"""Checked layer-transform composition and canvas-crop commands."""

from __future__ import annotations

import math

from kilix_image_shop.domain.commands import CropCanvas, SetTransform
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import (
    AffineTransform,
    Canvas,
    GeometryLimits,
    Rect,
)
from kilix_image_shop.domain.identifiers import LayerId, RevisionId
from kilix_image_shop.domain.layers import GroupLayer, PixelLayer, TextLayer


class TransformValidationError(ValueError):
    """A transform or crop is outside checked document geometry."""


def compose(left: AffineTransform, right: AffineTransform) -> AffineTransform:
    """Compose affine matrices as `left(right(point))`."""

    if not isinstance(left, AffineTransform) or not isinstance(right, AffineTransform):
        raise TransformValidationError("affine composition requires typed transforms")
    return AffineTransform(
        a=left.a * right.a + left.c * right.b,
        b=left.b * right.a + left.d * right.b,
        c=left.a * right.c + left.c * right.d,
        d=left.b * right.c + left.d * right.d,
        e=left.a * right.e + left.c * right.f + left.e,
        f=left.b * right.e + left.d * right.f + left.f,
    )


def translation(x: float, y: float) -> AffineTransform:
    return AffineTransform(e=x, f=y)


def scale(x: float, y: float) -> AffineTransform:
    return AffineTransform(a=x, d=y)


def rotation(degrees: float) -> AffineTransform:
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)):
        raise TransformValidationError("rotation angle must be numeric")
    radians = math.radians(float(degrees))
    return AffineTransform(
        a=math.cos(radians),
        b=math.sin(radians),
        c=-math.sin(radians),
        d=math.cos(radians),
    )


def set_transform(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    transform: AffineTransform,
) -> SetTransform:
    layer = state.layer_map.get(layer_id)
    if not isinstance(layer, (PixelLayer, TextLayer, GroupLayer)):
        raise TransformValidationError("transform target has no affine transform")
    return SetTransform(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        transform=transform,
    )


def crop_canvas(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    rectangle: Rect,
    limits: GeometryLimits,
) -> CropCanvas:
    if not isinstance(rectangle, Rect) or not isinstance(limits, GeometryLimits):
        raise TransformValidationError("crop requires checked rectangle and limits")
    limits.validate(rectangle.width, rectangle.height)
    canvas = Canvas(
        rectangle.width,
        rectangle.height,
        rectangle.x,
        rectangle.y,
    )
    if state.selection is not None and not state.selection.bounds.is_within(canvas.bounds):
        raise TransformValidationError("crop would leave the active selection outside")
    return CropCanvas(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        canvas=canvas,
    )
