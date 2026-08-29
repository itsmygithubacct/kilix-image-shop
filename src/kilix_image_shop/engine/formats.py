"""Closed tier policy for working pixels and foreground-alpha masks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kilix_image_shop.domain.color import AlphaAssociation
from kilix_image_shop.domain.identifiers import ObjectId

from .api import InvalidGraph, PixelFormat, PixelSpec, UnsupportedOperation


class RenderTier(StrEnum):
    H0 = "h0"
    QUALIFIED_HIGHER = "qualified-higher"


@dataclass(frozen=True, slots=True)
class TierFormatPolicy:
    """A fit-policy decision, separate from native babl conversion details."""

    tier: RenderTier
    working_format: PixelFormat

    def __post_init__(self) -> None:
        if not isinstance(self.tier, RenderTier):
            raise InvalidGraph("render tier must be closed")
        if self.working_format not in (PixelFormat.RGBA_U16, PixelFormat.RGBA_FLOAT):
            raise InvalidGraph("working format must be an RGBA format")
        if self.tier is RenderTier.H0 and self.working_format is not PixelFormat.RGBA_U16:
            raise UnsupportedOperation("H0 requires RGBA u16 working pixels")

    @classmethod
    def h0(cls) -> TierFormatPolicy:
        return cls(RenderTier.H0, PixelFormat.RGBA_U16)

    def colour_spec(
        self,
        profile_digest: ObjectId,
        *,
        alpha_association: AlphaAssociation = AlphaAssociation.STRAIGHT,
    ) -> PixelSpec:
        return PixelSpec.colour(
            self.working_format,
            profile_digest,
            alpha_association=alpha_association,
        )

    @property
    def mask_spec(self) -> PixelSpec:
        return PixelSpec.foreground_mask()

    def validate(self, spec: PixelSpec) -> None:
        if not isinstance(spec, PixelSpec):
            raise InvalidGraph("tier format policy requires a typed pixel spec")
        if spec.pixel_format is PixelFormat.Y_U8:
            if spec != self.mask_spec:
                raise UnsupportedOperation("tier admits only foreground-alpha Y u8 masks")
            return
        if spec.pixel_format is not self.working_format:
            raise UnsupportedOperation("pixel spec differs from the selected tier format")

    def expected_byte_count(self, width: int, height: int, spec: PixelSpec) -> int:
        self.validate(spec)
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            raise InvalidGraph("pixel dimensions must be positive integers")
        return width * height * spec.pixel_format.bytes_per_pixel

    def validate_payload(self, payload: bytes, width: int, height: int, spec: PixelSpec) -> None:
        if not isinstance(payload, bytes):
            raise InvalidGraph("pixel payload must be immutable bytes")
        if len(payload) != self.expected_byte_count(width, height, spec):
            raise InvalidGraph("pixel payload length does not match format and geometry")


__all__ = ("RenderTier", "TierFormatPolicy")
