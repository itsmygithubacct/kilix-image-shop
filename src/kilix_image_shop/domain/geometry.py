"""Checked document-space geometry with no native-library dependency."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Self

from .identifiers import DomainValidationError


MAX_COORDINATE = 2_147_483_647


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field} must be an integer")
    return value


def _positive(value: object, field: str) -> int:
    parsed = _integer(value, field)
    if parsed <= 0:
        raise DomainValidationError(f"{field} must be positive")
    return parsed


def _coordinate(value: object, field: str) -> int:
    parsed = _integer(value, field)
    if not -MAX_COORDINATE <= parsed <= MAX_COORDINATE:
        raise DomainValidationError(f"{field} is outside the checked coordinate range")
    return parsed


@dataclass(frozen=True, slots=True)
class GeometryLimits:
    max_width: int
    max_height: int
    max_pixels: int

    def __post_init__(self) -> None:
        _positive(self.max_width, "max_width")
        _positive(self.max_height, "max_height")
        _positive(self.max_pixels, "max_pixels")
        if self.max_width > MAX_COORDINATE or self.max_height > MAX_COORDINATE:
            raise DomainValidationError("geometry limit exceeds native coordinate range")

    def validate(self, width: int, height: int) -> None:
        if width > self.max_width or height > self.max_height:
            raise DomainValidationError("geometry exceeds the configured dimension limit")
        if width * height > self.max_pixels:
            raise DomainValidationError("geometry exceeds the configured pixel limit")


@dataclass(frozen=True, slots=True)
class Canvas:
    width: int
    height: int
    origin_x: int = 0
    origin_y: int = 0

    def __post_init__(self) -> None:
        width = _positive(self.width, "canvas width")
        height = _positive(self.height, "canvas height")
        origin_x = _coordinate(self.origin_x, "canvas origin_x")
        origin_y = _coordinate(self.origin_y, "canvas origin_y")
        if origin_x + width > MAX_COORDINATE:
            raise DomainValidationError("canvas right edge exceeds coordinate range")
        if origin_y + height > MAX_COORDINATE:
            raise DomainValidationError("canvas bottom edge exceeds coordinate range")

    @classmethod
    def from_data(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "width",
            "height",
            "originX",
            "originY",
        }:
            raise DomainValidationError("canvas has missing or unknown fields")
        return cls(
            width=_integer(value["width"], "canvas width"),
            height=_integer(value["height"], "canvas height"),
            origin_x=_integer(value["originX"], "canvas origin_x"),
            origin_y=_integer(value["originY"], "canvas origin_y"),
        )

    def to_data(self) -> dict[str, int]:
        return {
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
        }

    @property
    def bounds(self) -> Rect:
        return Rect(self.origin_x, self.origin_y, self.width, self.height)


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        x = _coordinate(self.x, "rectangle x")
        y = _coordinate(self.y, "rectangle y")
        width = _positive(self.width, "rectangle width")
        height = _positive(self.height, "rectangle height")
        if x + width > MAX_COORDINATE or y + height > MAX_COORDINATE:
            raise DomainValidationError("rectangle edge exceeds coordinate range")

    @classmethod
    def from_data(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
            raise DomainValidationError("rectangle has missing or unknown fields")
        return cls(
            x=_integer(value["x"], "rectangle x"),
            y=_integer(value["y"], "rectangle y"),
            width=_integer(value["width"], "rectangle width"),
            height=_integer(value["height"], "rectangle height"),
        )

    def to_data(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    def is_within(self, other: Rect) -> bool:
        return (
            self.x >= other.x
            and self.y >= other.y
            and self.x + self.width <= other.x + other.width
            and self.y + self.height <= other.y + other.height
        )


@dataclass(frozen=True, slots=True)
class AffineTransform:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def __post_init__(self) -> None:
        values = (self.a, self.b, self.c, self.d, self.e, self.f)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise DomainValidationError("affine coefficients must be real numbers")
        normalized = tuple(0.0 if float(value) == 0.0 else float(value) for value in values)
        if not all(math.isfinite(value) for value in normalized):
            raise DomainValidationError("affine coefficients must be finite")
        if normalized[0] * normalized[3] - normalized[1] * normalized[2] == 0.0:
            raise DomainValidationError("affine transform must be invertible")
        for field, value in zip(("a", "b", "c", "d", "e", "f"), normalized, strict=True):
            object.__setattr__(self, field, value)

    @classmethod
    def from_data(cls, value: object) -> Self:
        if not isinstance(value, list) or len(value) != 6:
            raise DomainValidationError("affine transform must contain 6 coefficients")
        return cls(*value)

    def to_data(self) -> list[float]:
        return [self.a, self.b, self.c, self.d, self.e, self.f]
