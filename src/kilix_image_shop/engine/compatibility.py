"""Frozen OD-7 identity and exact project/runtime compatibility checks."""

from __future__ import annotations

import json
import math
import pathlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Self, TypeAlias

from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.identifiers import ObjectId
from kilix_image_shop.domain.layers import AdjustmentId, BlendMode

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

_OBSERVED_OPERATION_RE = re.compile(
    r"(?:gegl|svg):[a-z0-9]+(?:-[a-z0-9]+)*\Z"
)
_SEMANTIC_KEY_RE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+\Z")
_PROPERTY_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_PAD_RE = re.compile(r"[a-z][a-z0-9-]*\Z")
_NATIVE_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*\Z")


class RegistryFamily(StrEnum):
    AFFINE_RESAMPLING = "affine-resampling"
    OPACITY_BLEND = "opacity-blend"
    MASK_APPLICATION = "mask-application"
    ADJUSTMENT = "adjustment"
    ICC_CONVERSION = "icc-conversion"
    TEXT_RASTER = "text-raster"
    IMPORT_EXPORT = "import-export"
    DESTINATION = "destination"


class RegistryValueKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    NUMBER_VECTOR = "number-vector"
    STRING = "string"
    BUFFER = "buffer"
    PROFILE_PATH = "profile-path"


class PropertySource(StrEnum):
    DEFAULT = "default"
    FIXED = "fixed"
    SEMANTIC = "semantic"


RegistryScalar: TypeAlias = bool | int | float | str | None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _registry_scalar(value: object, field_name: str) -> RegistryScalar:
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str) and (not value or len(value) > 4096):
            raise InvalidGraph(f"{field_name} string is empty or too large")
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return 0.0 if value == 0.0 else value
    raise InvalidGraph(f"{field_name} must be a finite JSON scalar")


@dataclass(frozen=True, slots=True)
class OperationProperty:
    native_name: str
    native_type: str
    value_kind: RegistryValueKind
    source: PropertySource
    default_value: RegistryScalar
    semantic_name: str | None = None
    fixed_value: RegistryScalar = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.native_name, str)
            or _PROPERTY_RE.fullmatch(self.native_name) is None
        ):
            raise InvalidGraph("native property name is not canonical")
        if (
            not isinstance(self.native_type, str)
            or _NATIVE_TYPE_RE.fullmatch(self.native_type) is None
        ):
            raise InvalidGraph("native property type is not canonical")
        if not isinstance(self.value_kind, RegistryValueKind) or not isinstance(
            self.source, PropertySource
        ):
            raise InvalidGraph("operation property uses an unknown closed value")
        object.__setattr__(
            self,
            "default_value",
            _registry_scalar(self.default_value, "native default"),
        )
        object.__setattr__(
            self,
            "fixed_value",
            _registry_scalar(self.fixed_value, "native fixed value"),
        )
        if self.source is PropertySource.SEMANTIC:
            if (
                not isinstance(self.semantic_name, str)
                or _PROPERTY_RE.fullmatch(self.semantic_name) is None
            ):
                raise InvalidGraph("semantic property name is not canonical")
            if self.fixed_value is not None:
                raise InvalidGraph("semantic property cannot also be fixed")
        elif self.semantic_name is not None:
            raise InvalidGraph("non-semantic property cannot name a semantic value")
        if self.source is PropertySource.FIXED and self.fixed_value is None:
            raise InvalidGraph("fixed property requires a non-null value")
        if self.source is PropertySource.DEFAULT and self.fixed_value is not None:
            raise InvalidGraph("default property cannot carry a fixed value")
        if self.value_kind in {
            RegistryValueKind.BUFFER,
            RegistryValueKind.PROFILE_PATH,
        } and self.source is not PropertySource.SEMANTIC:
            raise InvalidGraph("runtime references must be semantic properties")

    def to_data(self) -> dict[str, object]:
        return {
            "nativeName": self.native_name,
            "nativeType": self.native_type,
            "valueKind": self.value_kind.value,
            "source": self.source.value,
            "default": self.default_value,
            "semanticName": self.semantic_name,
            "fixed": self.fixed_value,
        }

    @classmethod
    def from_data(cls, value: object) -> Self:
        required = {
            "nativeName",
            "nativeType",
            "valueKind",
            "source",
            "default",
            "semanticName",
            "fixed",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise InvalidGraph("operation property has missing or unknown fields")
        try:
            value_kind = RegistryValueKind(value["valueKind"])
            source = PropertySource(value["source"])
        except (TypeError, ValueError) as exc:
            raise InvalidGraph("operation property has an unknown closed value") from exc
        return cls(
            native_name=value["nativeName"],
            native_type=value["nativeType"],
            value_kind=value_kind,
            source=source,
            default_value=value["default"],
            semantic_name=value["semanticName"],
            fixed_value=value["fixed"],
        )


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    semantic_key: str
    family: RegistryFamily
    operation: str
    properties: tuple[OperationProperty, ...]
    input_pads: tuple[str, ...]
    output_pad: str | None
    halo_pixels: int
    golden_fixture_digest: ObjectId

    def __post_init__(self) -> None:
        if (
            not isinstance(self.semantic_key, str)
            or _SEMANTIC_KEY_RE.fullmatch(self.semantic_key) is None
        ):
            raise InvalidGraph("operation semantic key is not canonical")
        if not isinstance(self.family, RegistryFamily):
            raise InvalidGraph("operation registry family is not closed")
        if (
            not isinstance(self.operation, str)
            or _OBSERVED_OPERATION_RE.fullmatch(self.operation) is None
        ):
            raise InvalidGraph("native operation identity is not canonical")
        if not isinstance(self.properties, tuple) or any(
            not isinstance(item, OperationProperty) for item in self.properties
        ):
            raise InvalidGraph("operation properties must be an immutable typed tuple")
        property_names = tuple(item.native_name for item in self.properties)
        semantic_names = tuple(
            item.semantic_name
            for item in self.properties
            if item.semantic_name is not None
        )
        if property_names != tuple(sorted(set(property_names))):
            raise InvalidGraph("native operation properties must be sorted and unique")
        if len(semantic_names) != len(set(semantic_names)):
            raise InvalidGraph("operation repeats a semantic property")
        if not isinstance(self.input_pads, tuple) or any(
            not isinstance(item, str) or _PAD_RE.fullmatch(item) is None
            for item in self.input_pads
        ):
            raise InvalidGraph("operation input pads must be an immutable canonical tuple")
        if len(self.input_pads) != len(set(self.input_pads)):
            raise InvalidGraph("operation input pads must be unique")
        if self.output_pad is not None and (
            not isinstance(self.output_pad, str)
            or _PAD_RE.fullmatch(self.output_pad) is None
        ):
            raise InvalidGraph("operation output pad is not canonical")
        if (
            isinstance(self.halo_pixels, bool)
            or not isinstance(self.halo_pixels, int)
            or self.halo_pixels < 0
        ):
            raise InvalidGraph("operation halo must be a non-negative integer")
        if not isinstance(self.golden_fixture_digest, ObjectId):
            raise InvalidGraph("operation golden fixture must be content-addressed")

    @property
    def semantic_properties(self) -> tuple[OperationProperty, ...]:
        return tuple(
            item for item in self.properties if item.source is PropertySource.SEMANTIC
        )

    def to_data(self) -> dict[str, object]:
        return {
            "semanticKey": self.semantic_key,
            "family": self.family.value,
            "operation": self.operation,
            "properties": [item.to_data() for item in self.properties],
            "inputPads": list(self.input_pads),
            "outputPad": self.output_pad,
            "haloPixels": self.halo_pixels,
            "goldenFixtureSha256": self.golden_fixture_digest.value,
        }

    @classmethod
    def from_data(cls, value: object) -> Self:
        required = {
            "semanticKey",
            "family",
            "operation",
            "properties",
            "inputPads",
            "outputPad",
            "haloPixels",
            "goldenFixtureSha256",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise InvalidGraph("operation definition has missing or unknown fields")
        if not isinstance(value["properties"], list) or not isinstance(
            value["inputPads"], list
        ):
            raise InvalidGraph("operation definition collections must be lists")
        try:
            family = RegistryFamily(value["family"])
        except (TypeError, ValueError) as exc:
            raise InvalidGraph("operation definition family is unknown") from exc
        return cls(
            semantic_key=value["semanticKey"],
            family=family,
            operation=value["operation"],
            properties=tuple(
                OperationProperty.from_data(item) for item in value["properties"]
            ),
            input_pads=tuple(value["inputPads"]),
            output_pad=value["outputPad"],
            halo_pixels=value["haloPixels"],
            golden_fixture_digest=ObjectId.parse(value["goldenFixtureSha256"]),
        )


_FORMAT_NAMES = ("jpeg", "png", "tiff", "webp")
REQUIRED_OPERATION_KEYS: tuple[str, ...] = tuple(
    sorted(
        (
            "source.pixel",
            "source.text-raster",
            "sink.write-buffer",
            "transform.affine",
            "transform.crop",
            "blend.opacity",
            *(f"blend.mode.{item.value}" for item in BlendMode),
            "mask.apply",
            "mask.invert",
            *(f"adjustment.{item.value}" for item in AdjustmentId),
            "group.compose",
            "colour.cast",
            "colour.convert",
            "destination.scale",
            "destination.crop",
            *(f"import.{item}" for item in _FORMAT_NAMES),
            *(f"export.{item}" for item in _FORMAT_NAMES),
        )
    )
)


def _expected_family(semantic_key: str) -> RegistryFamily:
    if semantic_key.startswith("transform."):
        return RegistryFamily.AFFINE_RESAMPLING
    if semantic_key.startswith(("blend.", "group.")):
        return RegistryFamily.OPACITY_BLEND
    if semantic_key.startswith("mask."):
        return RegistryFamily.MASK_APPLICATION
    if semantic_key.startswith("adjustment."):
        return RegistryFamily.ADJUSTMENT
    if semantic_key.startswith("colour."):
        return RegistryFamily.ICC_CONVERSION
    if semantic_key == "source.text-raster":
        return RegistryFamily.TEXT_RASTER
    if semantic_key.startswith(("source.", "sink.", "import.", "export.")):
        return RegistryFamily.IMPORT_EXPORT
    if semantic_key.startswith("destination."):
        return RegistryFamily.DESTINATION
    raise InvalidGraph("operation semantic key has no closed registry family")


@dataclass(frozen=True, slots=True)
class OperationRegistry:
    schema: str
    definitions: tuple[OperationDefinition, ...]

    SCHEMA: ClassVar[str] = "kilix.imageshop.operation-registry/v1"

    def __post_init__(self) -> None:
        if self.schema != self.SCHEMA:
            raise InvalidGraph("unsupported operation-registry schema")
        if not isinstance(self.definitions, tuple) or any(
            not isinstance(item, OperationDefinition) for item in self.definitions
        ):
            raise InvalidGraph("operation definitions must be an immutable typed tuple")
        keys = tuple(item.semantic_key for item in self.definitions)
        if keys != tuple(sorted(set(keys))):
            raise InvalidGraph("operation definitions must be sorted and unique")
        if keys != REQUIRED_OPERATION_KEYS:
            raise InvalidGraph("operation registry does not cover the exact semantic map")
        if {item.family for item in self.definitions} != set(RegistryFamily):
            raise InvalidGraph("operation registry does not cover all eight families")
        for item in self.definitions:
            if item.family is not _expected_family(item.semantic_key):
                raise InvalidGraph("operation definition is assigned to the wrong family")
        if not set(MINIMUM_OPERATIONS) <= set(self.native_operations):
            raise InvalidGraph("operation registry omits a qualified OD-7 operation")

    @property
    def native_operations(self) -> tuple[str, ...]:
        return tuple(sorted({item.operation for item in self.definitions}))

    def definition(self, semantic_key: str) -> OperationDefinition:
        for item in self.definitions:
            if item.semantic_key == semantic_key:
                return item
        raise InvalidGraph("operation semantic key is outside the accepted registry")

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "definitions": [item.to_data() for item in self.definitions],
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_data(),
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes, *, maximum_bytes: int = 4_194_304) -> Self:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= maximum_bytes:
            raise InvalidGraph("operation-registry carrier has an invalid byte size")
        try:
            value = json.loads(payload, object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise InvalidGraph("operation-registry carrier is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "definitions"}:
            raise InvalidGraph("operation registry has missing or unknown fields")
        if not isinstance(value["definitions"], list):
            raise InvalidGraph("operation registry definitions must be a list")
        return cls(
            schema=value["schema"],
            definitions=tuple(
                OperationDefinition.from_data(item) for item in value["definitions"]
            ),
        )


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
    operation_registry: OperationRegistry
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

        if not isinstance(self.operation_registry, OperationRegistry):
            raise InvalidGraph("runtime configuration requires an operation registry")
        if self.operation_registry.digest != expected.operation_set_digest:
            raise InvalidGraph("operation-registry digest does not match policy")

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
        for operation in configuration.operation_registry.native_operations
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
    "OperationDefinition",
    "OperationProperty",
    "OperationRegistry",
    "PACKAGE_GROUP_ID",
    "PYTHON_GI_PACKAGE_VERSION",
    "PropertySource",
    "REQUIRED_OPERATION_KEYS",
    "RegistryFamily",
    "RegistryValueKind",
    "RuntimeConfiguration",
    "compatibility_differences",
    "require_compatible",
    "validate_native_observation",
)
