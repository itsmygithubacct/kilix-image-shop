"""Normalized, versioned deterministic export presets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Self, TypeAlias

from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import MAX_COORDINATE, Rect
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    ObjectId,
    RevisionId,
)
from kilix_image_shop.domain.layers import GroupLayer, TextLayer
from kilix_image_shop.engine.api import PixelFormat


EXPORT_PRESET_SCHEMA = "kilix.imageshop.export-preset/v1"
MAX_PRESET_BYTES = 262_144
DETERMINISM_BINDINGS: tuple[str, ...] = (
    "babl-tolerance",
    "working-format",
    "engine-compatibility",
    "plugin-tree",
    "document-generation-closure",
    "working-output-profiles",
    "crop-dimensions-resampling",
    "alpha-metadata",
    "codec-settings",
    "locale-timezone-environment",
)
NORMALIZED_ENVIRONMENT: tuple[tuple[str, str], ...] = (
    ("BABL_TOLERANCE", "0.0"),
    ("LC_ALL", "C.UTF-8"),
    ("TZ", "UTC"),
)


class ExportPresetError(ValueError):
    """A deterministic export preset is malformed or incompatible."""


class ExportFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    TIFF = "tiff"


class AlphaPolicy(StrEnum):
    PRESERVE = "preserve"
    OPAQUE_BACKGROUND = "opaque-background"


class MetadataPolicy(StrEnum):
    STRIP = "strip"
    PRESERVE = "preserve"


SettingValue: TypeAlias = bool | int | str
_CODE_RE = re.compile(r"[a-z][a-z0-9.-]*(?:/[v][1-9][0-9]*)\Z")


_NORMALIZED_SETTINGS: dict[ExportFormat, tuple[tuple[str, SettingValue], ...]] = {
    ExportFormat.PNG: (
        ("compression-level", 9),
        ("interlace", False),
    ),
    ExportFormat.JPEG: (
        ("chroma-subsampling", "4:4:4"),
        ("optimize", False),
        ("progressive", False),
        ("quality", 95),
    ),
    ExportFormat.WEBP: (
        ("exact-alpha", True),
        ("lossless", True),
        ("method", 6),
    ),
    ExportFormat.TIFF: (
        ("compression", "deflate"),
        ("predictor", "horizontal"),
    ),
}


@dataclass(frozen=True, slots=True)
class EncoderIdentity:
    export_format: ExportFormat
    codec_id: str
    package_digest: ObjectId
    settings: tuple[tuple[str, SettingValue], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.export_format, ExportFormat):
            raise ExportPresetError("encoder format is outside the closed set")
        if not isinstance(self.codec_id, str) or _CODE_RE.fullmatch(self.codec_id) is None:
            raise ExportPresetError("codec identity is not canonical")
        if not isinstance(self.package_digest, ObjectId):
            raise ExportPresetError("codec package identity must be content-addressed")
        if not isinstance(self.settings, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or isinstance(item[1], float)
            or not isinstance(item[1], (bool, int, str))
            for item in self.settings
        ):
            raise ExportPresetError("encoder settings must be immutable scalar pairs")
        if self.settings != _NORMALIZED_SETTINGS[self.export_format]:
            raise ExportPresetError("encoder settings differ from the normalized preset")
        expected = f"kilix.codec.{self.export_format.value}/v1"
        if self.codec_id != expected:
            raise ExportPresetError("codec identity differs from its output format")

    @classmethod
    def normalized(
        cls,
        export_format: ExportFormat,
        package_digest: ObjectId,
    ) -> EncoderIdentity:
        if not isinstance(export_format, ExportFormat):
            raise ExportPresetError("encoder format is outside the closed set")
        return cls(
            export_format,
            f"kilix.codec.{export_format.value}/v1",
            package_digest,
            _NORMALIZED_SETTINGS[export_format],
        )

    @classmethod
    def from_data(cls, value: object) -> EncoderIdentity:
        if not isinstance(value, dict) or set(value) != {
            "format",
            "id",
            "packageSha256",
            "settings",
        }:
            raise ExportPresetError("encoder identity has missing or unknown fields")
        raw_settings = value["settings"]
        if not isinstance(raw_settings, list):
            raise ExportPresetError("encoder settings must be a list")
        settings: list[tuple[str, SettingValue]] = []
        for item in raw_settings:
            if not isinstance(item, dict) or set(item) != {"name", "value"}:
                raise ExportPresetError("encoder setting is malformed")
            settings.append((item["name"], item["value"]))
        try:
            export_format = ExportFormat(value["format"])
            package_digest = ObjectId.parse(value["packageSha256"])
        except (TypeError, ValueError) as exc:
            raise ExportPresetError("encoder identity contains an invalid value") from exc
        return cls(export_format, value["id"], package_digest, tuple(settings))

    def to_data(self) -> dict[str, object]:
        return {
            "format": self.export_format.value,
            "id": self.codec_id,
            "packageSha256": self.package_digest.value,
            "settings": [
                {"name": name, "value": value} for name, value in self.settings
            ],
        }


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExportPresetError(f"duplicate export-preset member: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExportPresetError(f"non-finite export-preset number is forbidden: {value}")


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ExportPresetError("export preset cannot be serialized canonically") from exc


def _parse_canonical(payload: bytes) -> object:
    if not isinstance(payload, bytes):
        raise ExportPresetError("export preset must be immutable bytes")
    if len(payload) > MAX_PRESET_BYTES:
        raise ExportPresetError("export preset exceeds its finite byte budget")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExportPresetError("export preset is not strict UTF-8 JSON") from exc
    if _canonical(value) != payload:
        raise ExportPresetError("export preset is not in canonical form")
    return value


def document_object_ids(document: DocumentState) -> tuple[ObjectId, ...]:
    """Return the complete sorted object closure that can affect document output."""

    if not isinstance(document, DocumentState):
        raise ExportPresetError("object closure requires an immutable document")
    values: set[ObjectId] = {
        document.colour.working_profile,
        document.engine_compatibility.working_profile,
    }
    for asset in document.assets:
        values.update((asset.digest, asset.profile_digest))
    if document.selection is not None:
        values.add(document.selection.object_id)
    for layer in document.layers:
        mask = getattr(layer, "mask", None)
        if mask is not None:
            values.add(mask.object_id)
            if mask.source_ref is not None:
                values.add(mask.source_ref)
        operation = getattr(layer, "operation_provenance", None)
        if operation is not None:
            values.add(operation.runtime_digest)
            if operation.model_digest is not None:
                values.add(operation.model_digest)
            if operation.source_layer_digest is not None:
                values.add(operation.source_layer_digest)
        if isinstance(layer, TextLayer):
            values.update((layer.font_digest, layer.preview_asset_digest))
            values.update(
                item.resolved_font_digest
                for item in layer.fallbacks
                if item.resolved_font_digest is not None
            )
        if isinstance(layer, GroupLayer):
            # Child IDs are document structure, not content objects.
            continue
    for operation in document.provenance:
        values.add(operation.runtime_digest)
        if operation.model_digest is not None:
            values.add(operation.model_digest)
        if operation.source_layer_digest is not None:
            values.add(operation.source_layer_digest)
    return tuple(sorted(values, key=lambda item: item.value))


def object_closure_digest(document: DocumentState) -> ObjectId:
    payload = _canonical([item.value for item in document_object_ids(document)])
    return ObjectId.from_bytes(payload)


@dataclass(frozen=True, slots=True)
class ExportPreset:
    schema: str
    document_id: DocumentId
    revision: RevisionId
    document_manifest_digest: ObjectId
    generation_digest: ObjectId
    object_closure_digest: ObjectId
    compatibility_digest: ObjectId
    plugin_tree_digest: ObjectId
    working_format: PixelFormat
    working_profile: ObjectId
    output_profile: ObjectId
    crop: Rect
    width: int
    height: int
    resampling_kernel: str
    alpha_policy: AlphaPolicy
    background_rgb_u16: tuple[int, int, int] | None
    metadata_policy: MetadataPolicy
    encoder: EncoderIdentity
    locale: str = "C.UTF-8"
    timezone: str = "UTC"
    environment: tuple[tuple[str, str], ...] = NORMALIZED_ENVIRONMENT

    def __post_init__(self) -> None:
        if self.schema != EXPORT_PRESET_SCHEMA:
            raise ExportPresetError("unsupported export-preset schema")
        if not isinstance(self.document_id, DocumentId) or not isinstance(
            self.revision, RevisionId
        ):
            raise ExportPresetError("export preset lacks document identity")
        for field in (
            "document_manifest_digest",
            "generation_digest",
            "object_closure_digest",
            "compatibility_digest",
            "plugin_tree_digest",
            "working_profile",
            "output_profile",
        ):
            if not isinstance(getattr(self, field), ObjectId):
                raise ExportPresetError(f"{field} must be content-addressed")
        if not isinstance(self.working_format, PixelFormat) or self.working_format not in {
            PixelFormat.RGBA_U16,
            PixelFormat.RGBA_FLOAT,
        }:
            raise ExportPresetError("export working format must be profiled RGBA")
        if not isinstance(self.crop, Rect):
            raise ExportPresetError("export crop must be checked geometry")
        for value, label in ((self.width, "width"), (self.height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExportPresetError(f"export {label} must be positive")
            if value > MAX_COORDINATE:
                raise ExportPresetError(f"export {label} exceeds native geometry")
        if (
            not isinstance(self.resampling_kernel, str)
            or not self.resampling_kernel
            or len(self.resampling_kernel) > 128
            or any(ord(item) < 0x20 or ord(item) == 0x7F for item in self.resampling_kernel)
        ):
            raise ExportPresetError("export resampling policy is not canonical")
        if not isinstance(self.alpha_policy, AlphaPolicy) or not isinstance(
            self.metadata_policy, MetadataPolicy
        ):
            raise ExportPresetError("export alpha or metadata policy is outside its closed set")
        if self.alpha_policy is AlphaPolicy.PRESERVE:
            if self.background_rgb_u16 is not None:
                raise ExportPresetError("preserved alpha cannot carry an opaque background")
            if self.encoder.export_format is ExportFormat.JPEG:
                raise ExportPresetError("JPEG export requires an opaque background")
        else:
            if (
                not isinstance(self.background_rgb_u16, tuple)
                or len(self.background_rgb_u16) != 3
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or not 0 <= item <= 65535
                    for item in self.background_rgb_u16
                )
            ):
                raise ExportPresetError("opaque export requires an RGB u16 background")
        if not isinstance(self.encoder, EncoderIdentity):
            raise ExportPresetError("export preset requires normalized encoder identity")
        if self.locale != "C.UTF-8" or self.timezone != "UTC":
            raise ExportPresetError("deterministic locale and timezone are fixed")
        if self.environment != NORMALIZED_ENVIRONMENT:
            raise ExportPresetError("deterministic export environment is fixed")

    @property
    def export_format(self) -> ExportFormat:
        return self.encoder.export_format

    @property
    def binding_groups(self) -> tuple[str, ...]:
        return DETERMINISM_BINDINGS

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "document": {
                "documentId": self.document_id.value,
                "revisionId": self.revision.value,
                "manifestSha256": self.document_manifest_digest.value,
                "generationSha256": self.generation_digest.value,
                "objectClosureSha256": self.object_closure_digest.value,
            },
            "engine": {
                "compatibilitySha256": self.compatibility_digest.value,
                "pluginTreeSha256": self.plugin_tree_digest.value,
                "workingFormat": self.working_format.value,
            },
            "profiles": {
                "workingSha256": self.working_profile.value,
                "outputSha256": self.output_profile.value,
            },
            "geometry": {
                "crop": self.crop.to_data(),
                "width": self.width,
                "height": self.height,
                "resamplingKernel": self.resampling_kernel,
            },
            "alphaMetadata": {
                "alphaPolicy": self.alpha_policy.value,
                "backgroundRgbU16": (
                    None
                    if self.background_rgb_u16 is None
                    else list(self.background_rgb_u16)
                ),
                "metadataPolicy": self.metadata_policy.value,
            },
            "codec": self.encoder.to_data(),
            "environment": {
                "locale": self.locale,
                "timezone": self.timezone,
                "variables": [
                    {"name": name, "value": value}
                    for name, value in self.environment
                ],
            },
            "determinismBindings": list(self.binding_groups),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_data())

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())

    @classmethod
    def from_data(cls, value: object) -> ExportPreset:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "document",
            "engine",
            "profiles",
            "geometry",
            "alphaMetadata",
            "codec",
            "environment",
            "determinismBindings",
        }:
            raise ExportPresetError("export preset has missing or unknown fields")
        document = value["document"]
        engine = value["engine"]
        profiles = value["profiles"]
        geometry = value["geometry"]
        alpha = value["alphaMetadata"]
        environment = value["environment"]
        if not isinstance(document, dict) or set(document) != {
            "documentId",
            "revisionId",
            "manifestSha256",
            "generationSha256",
            "objectClosureSha256",
        }:
            raise ExportPresetError("export document binding is malformed")
        if not isinstance(engine, dict) or set(engine) != {
            "compatibilitySha256",
            "pluginTreeSha256",
            "workingFormat",
        }:
            raise ExportPresetError("export engine binding is malformed")
        if not isinstance(profiles, dict) or set(profiles) != {
            "workingSha256",
            "outputSha256",
        }:
            raise ExportPresetError("export profile binding is malformed")
        if not isinstance(geometry, dict) or set(geometry) != {
            "crop",
            "width",
            "height",
            "resamplingKernel",
        }:
            raise ExportPresetError("export geometry binding is malformed")
        if not isinstance(alpha, dict) or set(alpha) != {
            "alphaPolicy",
            "backgroundRgbU16",
            "metadataPolicy",
        }:
            raise ExportPresetError("export alpha/metadata binding is malformed")
        if not isinstance(environment, dict) or set(environment) != {
            "locale",
            "timezone",
            "variables",
        }:
            raise ExportPresetError("export environment binding is malformed")
        if value["determinismBindings"] != list(DETERMINISM_BINDINGS):
            raise ExportPresetError("export preset lacks all deterministic bindings")
        raw_variables = environment["variables"]
        if not isinstance(raw_variables, list):
            raise ExportPresetError("export environment variables are malformed")
        variables: list[tuple[str, str]] = []
        for item in raw_variables:
            if not isinstance(item, dict) or set(item) != {"name", "value"}:
                raise ExportPresetError("export environment variable is malformed")
            variables.append((item["name"], item["value"]))
        background = alpha["backgroundRgbU16"]
        if background is not None and (
            not isinstance(background, list) or len(background) != 3
        ):
            raise ExportPresetError("export background is malformed")
        try:
            return cls(
                schema=value["schema"],
                document_id=DocumentId.parse(document["documentId"]),
                revision=RevisionId.parse(document["revisionId"]),
                document_manifest_digest=ObjectId.parse(document["manifestSha256"]),
                generation_digest=ObjectId.parse(document["generationSha256"]),
                object_closure_digest=ObjectId.parse(
                    document["objectClosureSha256"]
                ),
                compatibility_digest=ObjectId.parse(engine["compatibilitySha256"]),
                plugin_tree_digest=ObjectId.parse(engine["pluginTreeSha256"]),
                working_format=PixelFormat(engine["workingFormat"]),
                working_profile=ObjectId.parse(profiles["workingSha256"]),
                output_profile=ObjectId.parse(profiles["outputSha256"]),
                crop=Rect.from_data(geometry["crop"]),
                width=geometry["width"],
                height=geometry["height"],
                resampling_kernel=geometry["resamplingKernel"],
                alpha_policy=AlphaPolicy(alpha["alphaPolicy"]),
                background_rgb_u16=(
                    None if background is None else tuple(background)
                ),
                metadata_policy=MetadataPolicy(alpha["metadataPolicy"]),
                encoder=EncoderIdentity.from_data(value["codec"]),
                locale=environment["locale"],
                timezone=environment["timezone"],
                environment=tuple(variables),
            )
        except (TypeError, ValueError) as exc:
            raise ExportPresetError("export preset contains an invalid value") from exc

    @classmethod
    def from_bytes(cls, payload: bytes) -> ExportPreset:
        return cls.from_data(_parse_canonical(payload))


def deterministic_preset(
    document: DocumentState,
    generation_digest: ObjectId,
    export_format: ExportFormat,
    *,
    crop: Rect | None = None,
    width: int | None = None,
    height: int | None = None,
    output_profile: ObjectId | None = None,
    alpha_policy: AlphaPolicy | None = None,
    background_rgb_u16: tuple[int, int, int] = (65535, 65535, 65535),
    metadata_policy: MetadataPolicy = MetadataPolicy.STRIP,
) -> ExportPreset:
    """Bind one immutable document generation to the normalized codec preset."""

    if not isinstance(document, DocumentState) or not isinstance(
        generation_digest, ObjectId
    ):
        raise ExportPresetError("preset creation requires document and generation identity")
    if not isinstance(export_format, ExportFormat):
        raise ExportPresetError("preset creation requires a closed export format")
    bounds = document.canvas.bounds if crop is None else crop
    if not isinstance(bounds, Rect) or not bounds.is_within(document.canvas.bounds):
        raise ExportPresetError("export crop must stay within the document canvas")
    output_width = bounds.width if width is None else width
    output_height = bounds.height if height is None else height
    policy = alpha_policy
    if policy is None:
        policy = (
            AlphaPolicy.OPAQUE_BACKGROUND
            if export_format is ExportFormat.JPEG
            else AlphaPolicy.PRESERVE
        )
    background = background_rgb_u16 if policy is AlphaPolicy.OPAQUE_BACKGROUND else None
    try:
        working_format = PixelFormat(document.engine_compatibility.working_format)
    except ValueError as exc:
        raise ExportPresetError("document working format is unsupported") from exc
    return ExportPreset(
        schema=EXPORT_PRESET_SCHEMA,
        document_id=document.document_id,
        revision=document.revision_id,
        document_manifest_digest=document.manifest_digest,
        generation_digest=generation_digest,
        object_closure_digest=object_closure_digest(document),
        compatibility_digest=document.engine_compatibility.digest,
        plugin_tree_digest=document.engine_compatibility.plugin_tree_digest,
        working_format=working_format,
        working_profile=document.colour.working_profile,
        output_profile=(
            document.colour.working_profile
            if output_profile is None
            else output_profile
        ),
        crop=bounds,
        width=output_width,
        height=output_height,
        resampling_kernel=document.engine_compatibility.resampling_kernel,
        alpha_policy=policy,
        background_rgb_u16=background,
        metadata_policy=metadata_policy,
        encoder=EncoderIdentity.normalized(
            export_format,
            document.engine_compatibility.package_group_digest,
        ),
    )


__all__ = (
    "AlphaPolicy",
    "DETERMINISM_BINDINGS",
    "EncoderIdentity",
    "ExportFormat",
    "ExportPreset",
    "ExportPresetError",
    "MetadataPolicy",
    "MAX_PRESET_BYTES",
    "NORMALIZED_ENVIRONMENT",
    "deterministic_preset",
    "document_object_ids",
    "object_closure_digest",
)
