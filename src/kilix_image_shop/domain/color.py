"""Explicit colour and render-compatibility values."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from .identifiers import DomainValidationError, ObjectId


class ColourSpace(StrEnum):
    SRGB = "srgb"
    DISPLAY_P3 = "display-p3"
    ADOBE_RGB_1998 = "adobe-rgb-1998"
    CUSTOM_ICC = "custom-icc"


class ConversionPolicy(StrEnum):
    RELATIVE_COLORIMETRIC = "relative-colorimetric"
    PERCEPTUAL = "perceptual"
    PRESERVE_NUMBERS = "preserve-numbers"


class AlphaAssociation(StrEnum):
    STRAIGHT = "straight"
    PREMULTIPLIED = "premultiplied"
    OPAQUE = "opaque"


def _enum(enum_type: type[StrEnum], value: object, field: str) -> StrEnum:
    if not isinstance(value, str):
        raise DomainValidationError(f"{field} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DomainValidationError(f"unsupported {field}: {value!r}") from exc


def _closed_string(value: object, field: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DomainValidationError(f"{field} must be a non-empty bounded string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise DomainValidationError(f"{field} contains a control character")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainValidationError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ColourState:
    working_profile: ObjectId
    declared_space: ColourSpace
    conversion_policy: ConversionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.working_profile, ObjectId):
            raise DomainValidationError("working profile must be content-addressed")
        if not isinstance(self.declared_space, ColourSpace):
            raise DomainValidationError("declared colour space must be closed")
        if not isinstance(self.conversion_policy, ConversionPolicy):
            raise DomainValidationError("colour conversion policy must be closed")

    @classmethod
    def from_data(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {
            "workingProfileSha256",
            "declaredSpace",
            "conversionPolicy",
        }:
            raise DomainValidationError("colour state has missing or unknown fields")
        return cls(
            working_profile=ObjectId.parse(value["workingProfileSha256"]),
            declared_space=_enum(
                ColourSpace, value["declaredSpace"], "declared colour space"
            ),
            conversion_policy=_enum(
                ConversionPolicy,
                value["conversionPolicy"],
                "colour conversion policy",
            ),
        )

    def to_data(self) -> dict[str, str]:
        return {
            "workingProfileSha256": self.working_profile.value,
            "declaredSpace": self.declared_space.value,
            "conversionPolicy": self.conversion_policy.value,
        }


_GROUP_ID_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+\Z")


@dataclass(frozen=True, slots=True)
class EngineCompatibility:
    """Persisted render contract; data only, never a plugin-load instruction."""

    schema: str
    package_group_id: str
    package_group_digest: ObjectId
    gegl_version: str
    babl_version: str
    python_gi_version: str
    gi_file_digest: ObjectId
    operation_count: int
    operation_set_digest: ObjectId
    plugin_tree_digest: ObjectId
    working_format: str
    alpha_association: AlphaAssociation
    mask_format: str
    mask_semantics: str
    working_profile: ObjectId
    conversion_policy: ConversionPolicy
    resampling_kernel: str
    edge_mode: str
    tile_halos: tuple[tuple[str, int], ...]
    use_opencl: bool
    tile_cache_bytes: int
    swap_compression: str
    threads: int
    deterministic_preset: str
    babl_tolerance: str

    SCHEMA = "kilix.imageshop.engine-compatibility/v1"

    def __post_init__(self) -> None:
        if self.schema != self.SCHEMA:
            raise DomainValidationError("unsupported engine compatibility schema")
        if not isinstance(self.package_group_id, str) or _GROUP_ID_RE.fullmatch(
            self.package_group_id
        ) is None:
            raise DomainValidationError("invalid package-group identity")
        for field in (
            "package_group_digest",
            "gi_file_digest",
            "operation_set_digest",
            "plugin_tree_digest",
            "working_profile",
        ):
            if not isinstance(getattr(self, field), ObjectId):
                raise DomainValidationError(f"{field} must be content-addressed")
        _closed_string(self.gegl_version, "GEGL version")
        _closed_string(self.babl_version, "babl version")
        _closed_string(self.python_gi_version, "python3-gi version")
        _positive_integer(self.operation_count, "operation count")
        if self.working_format not in {"RGBA u16", "RGBA float"}:
            raise DomainValidationError("unsupported working format")
        if not isinstance(self.alpha_association, AlphaAssociation):
            raise DomainValidationError("alpha association must be closed")
        if self.mask_format != "Y u8" or self.mask_semantics != "foreground-alpha":
            raise DomainValidationError("unsupported mask format or semantics")
        if not isinstance(self.conversion_policy, ConversionPolicy):
            raise DomainValidationError("colour conversion policy must be closed")
        _closed_string(self.resampling_kernel, "resampling kernel")
        _closed_string(self.edge_mode, "edge mode")
        if not isinstance(self.use_opencl, bool):
            raise DomainValidationError("use_opencl must be boolean")
        _positive_integer(self.tile_cache_bytes, "tile-cache bytes")
        if self.swap_compression not in {"fast", "zlib", "none"}:
            raise DomainValidationError("unsupported swap-compression value")
        _positive_integer(self.threads, "thread policy")
        _closed_string(self.deterministic_preset, "deterministic preset")
        if self.babl_tolerance != "0.0":
            raise DomainValidationError("deterministic compatibility requires 0.0")

        normalized_halos: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in self.tile_halos:
            if not isinstance(item, tuple) or len(item) != 2:
                raise DomainValidationError("tile halo must be a name/pixel pair")
            name = _closed_string(item[0], "tile-halo family")
            pixels = item[1]
            if isinstance(pixels, bool) or not isinstance(pixels, int) or pixels < 0:
                raise DomainValidationError("tile halo must be a non-negative integer")
            if name in seen:
                raise DomainValidationError("duplicate tile-halo family")
            seen.add(name)
            normalized_halos.append((name, pixels))
        normalized = tuple(sorted(normalized_halos))
        if normalized != self.tile_halos:
            object.__setattr__(self, "tile_halos", normalized)

    @classmethod
    def from_data(cls, value: object) -> Self:
        required = {
            "schema",
            "packageGroupId",
            "packageGroupSha256",
            "geglVersion",
            "bablVersion",
            "pythonGiVersion",
            "giFileSha256",
            "operationCount",
            "operationSetSha256",
            "pluginTreeSha256",
            "workingFormat",
            "alphaAssociation",
            "maskFormat",
            "maskSemantics",
            "workingProfileSha256",
            "conversionPolicy",
            "resamplingKernel",
            "edgeMode",
            "tileHalos",
            "useOpencl",
            "tileCacheBytes",
            "swapCompression",
            "threads",
            "deterministicPreset",
            "bablTolerance",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError(
                "engine compatibility has missing or unknown fields"
            )
        raw_halos = value["tileHalos"]
        if not isinstance(raw_halos, list):
            raise DomainValidationError("tileHalos must be a list")
        halos: list[tuple[str, int]] = []
        for raw in raw_halos:
            if not isinstance(raw, dict) or set(raw) != {"family", "pixels"}:
                raise DomainValidationError("invalid tile-halo record")
            halos.append((raw["family"], raw["pixels"]))
        return cls(
            schema=value["schema"],
            package_group_id=value["packageGroupId"],
            package_group_digest=ObjectId.parse(value["packageGroupSha256"]),
            gegl_version=value["geglVersion"],
            babl_version=value["bablVersion"],
            python_gi_version=value["pythonGiVersion"],
            gi_file_digest=ObjectId.parse(value["giFileSha256"]),
            operation_count=value["operationCount"],
            operation_set_digest=ObjectId.parse(value["operationSetSha256"]),
            plugin_tree_digest=ObjectId.parse(value["pluginTreeSha256"]),
            working_format=value["workingFormat"],
            alpha_association=_enum(
                AlphaAssociation, value["alphaAssociation"], "alpha association"
            ),
            mask_format=value["maskFormat"],
            mask_semantics=value["maskSemantics"],
            working_profile=ObjectId.parse(value["workingProfileSha256"]),
            conversion_policy=_enum(
                ConversionPolicy,
                value["conversionPolicy"],
                "colour conversion policy",
            ),
            resampling_kernel=value["resamplingKernel"],
            edge_mode=value["edgeMode"],
            tile_halos=tuple(halos),
            use_opencl=value["useOpencl"],
            tile_cache_bytes=value["tileCacheBytes"],
            swap_compression=value["swapCompression"],
            threads=value["threads"],
            deterministic_preset=value["deterministicPreset"],
            babl_tolerance=value["bablTolerance"],
        )

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "packageGroupId": self.package_group_id,
            "packageGroupSha256": self.package_group_digest.value,
            "geglVersion": self.gegl_version,
            "bablVersion": self.babl_version,
            "pythonGiVersion": self.python_gi_version,
            "giFileSha256": self.gi_file_digest.value,
            "operationCount": self.operation_count,
            "operationSetSha256": self.operation_set_digest.value,
            "pluginTreeSha256": self.plugin_tree_digest.value,
            "workingFormat": self.working_format,
            "alphaAssociation": self.alpha_association.value,
            "maskFormat": self.mask_format,
            "maskSemantics": self.mask_semantics,
            "workingProfileSha256": self.working_profile.value,
            "conversionPolicy": self.conversion_policy.value,
            "resamplingKernel": self.resampling_kernel,
            "edgeMode": self.edge_mode,
            "tileHalos": [
                {"family": family, "pixels": pixels}
                for family, pixels in self.tile_halos
            ],
            "useOpencl": self.use_opencl,
            "tileCacheBytes": self.tile_cache_bytes,
            "swapCompression": self.swap_compression,
            "threads": self.threads,
            "deterministicPreset": self.deterministic_preset,
            "bablTolerance": self.babl_tolerance,
        }

    def canonical_bytes(self) -> bytes:
        text = json.dumps(
            self.to_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId(hashlib.sha256(self.canonical_bytes()).hexdigest())
