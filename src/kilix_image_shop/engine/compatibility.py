"""Frozen OD-7 identity and exact project/runtime compatibility checks."""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from typing import ClassVar

from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.identifiers import ObjectId

from .api import IncompatibleRuntime, InvalidGraph


PACKAGE_GROUP_ID = "plebian.f115.image-engine"
GEGL_PACKAGE_VERSION = "1:0.4.62-2+deb13u2"
BABL_PACKAGE_VERSION = "1:0.1.114-2"
PYTHON_GI_PACKAGE_VERSION = "3.50.0-4+b1"
GEGL_NATIVE_VERSION = "0.4.62"
BABL_NATIVE_VERSION = "0.1.114"
EXPECTED_OPERATION_COUNT = 203
GI_ORIGIN = pathlib.Path("/usr/lib/python3/dist-packages/gi/__init__.py")
H0_TILE_CACHE_BYTES = 256 * 1024 * 1024

MINIMUM_OPERATIONS: tuple[str, ...] = (
    "gegl:brightness-contrast",
    "gegl:buffer-source",
    "gegl:crop",
    "gegl:jpg-save",
    "gegl:load",
    "gegl:png-save",
    "gegl:saturation",
    "gegl:scale-ratio",
    "gegl:tiff-save",
    "gegl:webp-save",
    "gegl:write-buffer",
)

_OPERATION_RE = re.compile(r"gegl:[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_OBSERVED_OPERATION_RE = re.compile(
    r"(?:gegl|svg):[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)


def canonical_operations(operations: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(operations, tuple) or not operations:
        raise InvalidGraph("required operations must be a non-empty immutable tuple")
    if any(
        not isinstance(item, str) or _OPERATION_RE.fullmatch(item) is None
        for item in operations
    ):
        raise InvalidGraph("required operation identity is not canonical")
    normalized = tuple(sorted(set(operations)))
    if normalized != operations:
        raise InvalidGraph("required operations must be sorted and unique")
    return normalized


def operation_registry_bytes(operations: tuple[str, ...]) -> bytes:
    normalized = canonical_operations(operations)
    return (
        json.dumps(
            {
                "schema": "kilix.imageshop.required-operations/v1",
                "operations": list(normalized),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def operation_registry_digest(operations: tuple[str, ...]) -> ObjectId:
    return ObjectId.from_bytes(operation_registry_bytes(operations))


@dataclass(frozen=True, slots=True)
class NativeObservation:
    """Identity collected from GI and the installed Debian package database."""

    gegl_native_version: str
    babl_native_version: str
    gegl_package_version: str
    babl_package_version: str
    python_gi_package_version: str
    gi_origin: pathlib.Path
    gi_file_digest: ObjectId
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "gegl_native_version",
            "babl_native_version",
            "gegl_package_version",
            "babl_package_version",
            "python_gi_package_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise IncompatibleRuntime(f"{field_name} is not a bounded identity")
        if not isinstance(self.gi_origin, pathlib.Path) or not self.gi_origin.is_absolute():
            raise IncompatibleRuntime("GI origin must be an absolute path")
        if not isinstance(self.gi_file_digest, ObjectId):
            raise IncompatibleRuntime("GI file identity must be content-addressed")
        if not isinstance(self.operations, tuple) or any(
            not isinstance(item, str)
            or _OBSERVED_OPERATION_RE.fullmatch(item) is None
            for item in self.operations
        ):
            raise IncompatibleRuntime("observed operation population is malformed")
        normalized = tuple(sorted(set(self.operations)))
        if normalized != self.operations:
            raise IncompatibleRuntime("observed operations must be sorted and unique")


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """All values that must validate before the process imports GI."""

    expected: EngineCompatibility
    required_operations: tuple[str, ...]
    package_group_record: pathlib.Path
    plugin_tree_manifest: pathlib.Path
    cache_root: pathlib.Path
    runtime_root: pathlib.Path
    mipmap_rendering: bool = True

    MAX_CARRIER_BYTES: ClassVar[int] = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.expected, EngineCompatibility):
            raise InvalidGraph("runtime configuration requires compatibility data")
        expected = self.expected
        fixed = {
            "package group": (expected.package_group_id, PACKAGE_GROUP_ID),
            "GEGL package": (expected.gegl_version, GEGL_PACKAGE_VERSION),
            "babl package": (expected.babl_version, BABL_PACKAGE_VERSION),
            "python3-gi package": (
                expected.python_gi_version,
                PYTHON_GI_PACKAGE_VERSION,
            ),
            "working format": (expected.working_format, "RGBA u16"),
            "mask format": (expected.mask_format, "Y u8"),
            "mask semantics": (expected.mask_semantics, "foreground-alpha"),
            "tile cache": (expected.tile_cache_bytes, H0_TILE_CACHE_BYTES),
            "swap compression": (expected.swap_compression, "fast"),
            "babl tolerance": (expected.babl_tolerance, "0.0"),
        }
        for field_name, (actual, required) in fixed.items():
            if actual != required:
                raise InvalidGraph(f"{field_name} differs from the H0 OD-7 policy")
        if expected.operation_count != EXPECTED_OPERATION_COUNT:
            raise InvalidGraph("operation count differs from the accepted closure")
        if expected.use_opencl is not False:
            raise InvalidGraph("H0 must disable OpenCL")
        if not isinstance(self.mipmap_rendering, bool) or not self.mipmap_rendering:
            raise InvalidGraph("H0 must enable mipmap rendering")

        required = canonical_operations(self.required_operations)
        if not set(MINIMUM_OPERATIONS) <= set(required):
            raise InvalidGraph("required registry omits a qualified OD-7 operation")
        if operation_registry_digest(required) != expected.operation_set_digest:
            raise InvalidGraph("required-operation registry digest does not match policy")

        for field_name in (
            "package_group_record",
            "plugin_tree_manifest",
            "cache_root",
            "runtime_root",
        ):
            path = getattr(self, field_name)
            if not isinstance(path, pathlib.Path) or not path.is_absolute():
                raise InvalidGraph(f"{field_name} must be an explicit absolute path")
        if self.cache_root == self.runtime_root:
            raise InvalidGraph("cache and runtime roots must be distinct")


@dataclass(frozen=True, slots=True)
class CompatibilityDifference:
    field: str
    stored: object
    installed: object


def compatibility_differences(
    stored: EngineCompatibility,
    installed: EngineCompatibility,
) -> tuple[CompatibilityDifference, ...]:
    if not isinstance(stored, EngineCompatibility) or not isinstance(
        installed, EngineCompatibility
    ):
        raise InvalidGraph("compatibility comparison requires typed values")
    stored_data = stored.to_data()
    installed_data = installed.to_data()
    return tuple(
        CompatibilityDifference(field, stored_data[field], installed_data[field])
        for field in sorted(stored_data)
        if stored_data[field] != installed_data[field]
    )


def require_compatible(
    stored: EngineCompatibility,
    installed: EngineCompatibility,
) -> None:
    differences = compatibility_differences(stored, installed)
    if differences:
        fields = ",".join(item.field for item in differences)
        raise IncompatibleRuntime(f"project render identity differs in fields: {fields}")


def validate_native_observation(
    configuration: RuntimeConfiguration,
    observation: NativeObservation,
) -> None:
    expected = configuration.expected
    comparisons: tuple[tuple[str, object, object], ...] = (
        ("GEGL native version", observation.gegl_native_version, GEGL_NATIVE_VERSION),
        ("babl native version", observation.babl_native_version, BABL_NATIVE_VERSION),
        ("GEGL package version", observation.gegl_package_version, expected.gegl_version),
        ("babl package version", observation.babl_package_version, expected.babl_version),
        (
            "python3-gi package version",
            observation.python_gi_package_version,
            expected.python_gi_version,
        ),
        ("GI origin", observation.gi_origin, GI_ORIGIN),
        ("GI file digest", observation.gi_file_digest, expected.gi_file_digest),
        ("operation count", len(observation.operations), expected.operation_count),
    )
    mismatches = tuple(name for name, actual, wanted in comparisons if actual != wanted)
    if mismatches:
        raise IncompatibleRuntime(
            "installed runtime identity mismatch: " + ",".join(mismatches)
        )
    missing = tuple(
        operation
        for operation in configuration.required_operations
        if operation not in observation.operations
    )
    if missing:
        raise IncompatibleRuntime("installed runtime omits a required operation")


__all__ = (
    "BABL_NATIVE_VERSION",
    "BABL_PACKAGE_VERSION",
    "CompatibilityDifference",
    "EXPECTED_OPERATION_COUNT",
    "GEGL_NATIVE_VERSION",
    "GEGL_PACKAGE_VERSION",
    "GI_ORIGIN",
    "H0_TILE_CACHE_BYTES",
    "MINIMUM_OPERATIONS",
    "NativeObservation",
    "PACKAGE_GROUP_ID",
    "PYTHON_GI_PACKAGE_VERSION",
    "RuntimeConfiguration",
    "compatibility_differences",
    "operation_registry_bytes",
    "operation_registry_digest",
    "require_compatible",
    "validate_native_observation",
)
