"""Immutable imported-asset references and explicit decode budgets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from .identifiers import DomainValidationError, ObjectId


class MediaType(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"
    TIFF = "image/tiff"


class ImportPolicy(StrEnum):
    COPIED = "copied"
    EXTERNAL_ABSOLUTE = "external-absolute"
    EXTERNAL_PORTABLE_RELATIVE = "external-portable-relative"


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainValidationError(f"{field} must be a positive integer")
    return value


def _media_type(value: object) -> MediaType:
    if not isinstance(value, str):
        raise DomainValidationError("media type must be a string")
    try:
        return MediaType(value)
    except ValueError as exc:
        raise DomainValidationError(f"unsupported media type: {value!r}") from exc


def _import_policy(value: object) -> ImportPolicy:
    if not isinstance(value, str):
        raise DomainValidationError("import policy must be a string")
    try:
        return ImportPolicy(value)
    except ValueError as exc:
        raise DomainValidationError(f"unsupported import policy: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class AssetRef:
    digest: ObjectId
    byte_count: int
    media_type: MediaType
    width: int
    height: int
    profile_digest: ObjectId
    import_policy: ImportPolicy
    locator: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.digest, ObjectId) or not isinstance(
            self.profile_digest, ObjectId
        ):
            raise DomainValidationError("asset digests must be content-addressed")
        if not isinstance(self.media_type, MediaType):
            raise DomainValidationError("asset media type must be closed")
        if not isinstance(self.import_policy, ImportPolicy):
            raise DomainValidationError("asset import policy must be closed")
        _positive_integer(self.byte_count, "asset byte count")
        _positive_integer(self.width, "asset width")
        _positive_integer(self.height, "asset height")
        if self.import_policy is ImportPolicy.COPIED:
            if self.locator is not None:
                raise DomainValidationError("copied asset must not retain a locator")
            return
        if not isinstance(self.locator, str) or not self.locator:
            raise DomainValidationError("external asset requires a locator")
        if len(self.locator.encode("utf-8")) > 4096:
            raise DomainValidationError("external locator exceeds 4096 UTF-8 bytes")
        if "\x00" in self.locator or "\n" in self.locator or "\r" in self.locator:
            raise DomainValidationError("external locator contains a forbidden character")
        path = PurePosixPath(self.locator)
        if ".." in path.parts:
            raise DomainValidationError("external locator contains parent traversal")
        if self.import_policy is ImportPolicy.EXTERNAL_ABSOLUTE:
            if not path.is_absolute():
                raise DomainValidationError("absolute external locator must be absolute")
        else:
            if path.is_absolute() or self.locator in {".", ""}:
                raise DomainValidationError("portable external locator must stay relative")

    @classmethod
    def from_data(cls, value: object) -> Self:
        required = {
            "sha256",
            "byteCount",
            "mediaType",
            "width",
            "height",
            "profileSha256",
            "importPolicy",
            "locator",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError("asset reference has missing or unknown fields")
        return cls(
            digest=ObjectId.parse(value["sha256"]),
            byte_count=value["byteCount"],
            media_type=_media_type(value["mediaType"]),
            width=value["width"],
            height=value["height"],
            profile_digest=ObjectId.parse(value["profileSha256"]),
            import_policy=_import_policy(value["importPolicy"]),
            locator=value["locator"],
        )

    def to_data(self) -> dict[str, object]:
        return {
            "sha256": self.digest.value,
            "byteCount": self.byte_count,
            "mediaType": self.media_type.value,
            "width": self.width,
            "height": self.height,
            "profileSha256": self.profile_digest.value,
            "importPolicy": self.import_policy.value,
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True)
class DecodeBudget:
    max_encoded_bytes: int
    max_width: int
    max_height: int
    max_pixels: int
    max_metadata_bytes: int
    max_frames: int

    def __post_init__(self) -> None:
        for field in (
            "max_encoded_bytes",
            "max_width",
            "max_height",
            "max_pixels",
            "max_metadata_bytes",
            "max_frames",
        ):
            _positive_integer(getattr(self, field), field)

    def validate(self, asset: AssetRef, *, metadata_bytes: int, frames: int = 1) -> None:
        if isinstance(metadata_bytes, bool) or not isinstance(metadata_bytes, int):
            raise DomainValidationError("metadata byte count must be an integer")
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise DomainValidationError("frame count must be an integer")
        if asset.byte_count > self.max_encoded_bytes:
            raise DomainValidationError("asset exceeds encoded-byte budget")
        if asset.width > self.max_width or asset.height > self.max_height:
            raise DomainValidationError("asset exceeds dimension budget")
        if asset.width * asset.height > self.max_pixels:
            raise DomainValidationError("asset exceeds decoded-pixel budget")
        if metadata_bytes < 0 or metadata_bytes > self.max_metadata_bytes:
            raise DomainValidationError("asset exceeds metadata budget")
        if frames <= 0 or frames > self.max_frames:
            raise DomainValidationError("asset exceeds frame budget")
