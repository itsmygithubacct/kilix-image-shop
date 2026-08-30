"""Versioned deterministic export-sidecar projection and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Self

from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    ObjectId,
    RevisionId,
)
from kilix_image_shop.domain.layers import OperationProvenance
from kilix_image_shop.render.plan import RenderPlan

from .presets import (
    ExportFormat,
    ExportPreset,
    MetadataPolicy,
    object_closure_digest,
)


EXPORT_PROVENANCE_SCHEMA = "kilix.imageshop.export-provenance/v1"
_METADATA_KEY_RE = re.compile(r"[a-z][a-z0-9.-]{0,127}\Z")


class ExportProvenanceError(ValueError):
    """An export sidecar is malformed or does not join its artifact."""


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
        raise ExportProvenanceError("export sidecar cannot be serialized canonically") from exc


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExportProvenanceError(f"duplicate export-sidecar member: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ExportProvenanceError(f"non-finite export-sidecar number is forbidden: {value}")


def _parse_canonical(payload: bytes, *, maximum_bytes: int) -> object:
    if not isinstance(payload, bytes):
        raise ExportProvenanceError("export sidecar must be immutable bytes")
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes <= 0
        or len(payload) > maximum_bytes
    ):
        raise ExportProvenanceError("export sidecar exceeds its finite byte budget")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ExportProvenanceError("export sidecar is not strict UTF-8 JSON") from exc
    if _canonical(value) != payload:
        raise ExportProvenanceError("export sidecar is not in canonical form")
    return value


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    image_digest: ObjectId
    byte_count: int
    export_format: ExportFormat
    width: int
    height: int
    profile_digest: ObjectId
    metadata_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.image_digest, ObjectId) or not isinstance(
            self.profile_digest, ObjectId
        ):
            raise ExportProvenanceError("export artifact lacks content identity")
        for value, label in (
            (self.byte_count, "byte count"),
            (self.width, "width"),
            (self.height, "height"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExportProvenanceError(f"artifact {label} must be positive")
        if not isinstance(self.export_format, ExportFormat):
            raise ExportProvenanceError("artifact format is outside the closed set")
        if not isinstance(self.metadata_keys, tuple) or any(
            not isinstance(item, str) or _METADATA_KEY_RE.fullmatch(item) is None
            for item in self.metadata_keys
        ):
            raise ExportProvenanceError("artifact metadata keys are not canonical")
        if self.metadata_keys != tuple(sorted(set(self.metadata_keys))):
            raise ExportProvenanceError("artifact metadata keys must be sorted and unique")

    @classmethod
    def from_data(cls, value: object) -> ExportArtifact:
        if not isinstance(value, dict) or set(value) != {
            "byteCount",
            "format",
            "height",
            "metadataKeys",
            "profileSha256",
            "sha256",
            "width",
        }:
            raise ExportProvenanceError("artifact record has missing or unknown fields")
        metadata = value["metadataKeys"]
        if not isinstance(metadata, list):
            raise ExportProvenanceError("artifact metadata table is malformed")
        try:
            return cls(
                image_digest=ObjectId.parse(value["sha256"]),
                byte_count=value["byteCount"],
                export_format=ExportFormat(value["format"]),
                width=value["width"],
                height=value["height"],
                profile_digest=ObjectId.parse(value["profileSha256"]),
                metadata_keys=tuple(metadata),
            )
        except (TypeError, ValueError) as exc:
            raise ExportProvenanceError("artifact record contains an invalid value") from exc

    def to_data(self) -> dict[str, object]:
        return {
            "byteCount": self.byte_count,
            "format": self.export_format.value,
            "height": self.height,
            "metadataKeys": list(self.metadata_keys),
            "profileSha256": self.profile_digest.value,
            "sha256": self.image_digest.value,
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class ExportOperationRecord:
    provenance: OperationProvenance
    prompt_included: bool
    mask_refs: tuple[ObjectId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, OperationProvenance):
            raise ExportProvenanceError("operation record requires typed provenance")
        if not isinstance(self.prompt_included, bool):
            raise ExportProvenanceError("prompt inclusion must be explicit")
        if not self.prompt_included and self.provenance.prompt is not None:
            object.__setattr__(
                self,
                "provenance",
                replace(self.provenance, prompt=None),
            )
        if not isinstance(self.mask_refs, tuple) or any(
            not isinstance(item, ObjectId) for item in self.mask_refs
        ):
            raise ExportProvenanceError("operation mask refs must be immutable identities")
        if self.mask_refs != tuple(
            sorted(set(self.mask_refs), key=lambda item: item.value)
        ):
            raise ExportProvenanceError("operation mask refs must be sorted and unique")

    @classmethod
    def from_data(cls, value: object) -> ExportOperationRecord:
        if not isinstance(value, dict) or set(value) != {
            "maskSha256",
            "promptIncluded",
            "provenance",
        }:
            raise ExportProvenanceError("operation record has missing or unknown fields")
        masks = value["maskSha256"]
        if not isinstance(masks, list):
            raise ExportProvenanceError("operation mask table is malformed")
        provenance_value = value["provenance"]
        if not isinstance(provenance_value, dict):
            raise ExportProvenanceError("operation provenance is malformed")
        included = value["promptIncluded"]
        if not isinstance(included, bool):
            raise ExportProvenanceError("operation prompt policy is malformed")
        if not included and provenance_value.get("prompt") is not None:
            raise ExportProvenanceError("excluded prompt was disclosed in the sidecar")
        try:
            return cls(
                OperationProvenance.from_data(provenance_value),
                included,
                tuple(ObjectId.parse(item) for item in masks),
            )
        except (TypeError, ValueError) as exc:
            raise ExportProvenanceError("operation record contains an invalid value") from exc

    def to_data(self) -> dict[str, object]:
        provenance = self.provenance.to_data()
        if not self.prompt_included:
            provenance["prompt"] = None
        return {
            "maskSha256": [item.value for item in self.mask_refs],
            "promptIncluded": self.prompt_included,
            "provenance": provenance,
        }


@dataclass(frozen=True, slots=True)
class ExportProvenance:
    schema: str
    document_id: DocumentId
    revision: RevisionId
    document_manifest_digest: ObjectId
    generation_digest: ObjectId
    object_closure_digest: ObjectId
    render_plan_digest: ObjectId
    preset_digest: ObjectId
    engine_compatibility: EngineCompatibility
    artifact: ExportArtifact
    operations: tuple[ExportOperationRecord, ...]
    credential_kind: str = "unsigned-sidecar"

    def __post_init__(self) -> None:
        if self.schema != EXPORT_PROVENANCE_SCHEMA:
            raise ExportProvenanceError("unsupported export-sidecar schema")
        if not isinstance(self.document_id, DocumentId) or not isinstance(
            self.revision, RevisionId
        ):
            raise ExportProvenanceError("export sidecar lacks document identity")
        for field in (
            "document_manifest_digest",
            "generation_digest",
            "object_closure_digest",
            "render_plan_digest",
            "preset_digest",
        ):
            if not isinstance(getattr(self, field), ObjectId):
                raise ExportProvenanceError(f"{field} must be content-addressed")
        if not isinstance(self.engine_compatibility, EngineCompatibility) or not isinstance(
            self.artifact, ExportArtifact
        ):
            raise ExportProvenanceError("export sidecar lacks engine or artifact identity")
        if not isinstance(self.operations, tuple) or any(
            not isinstance(item, ExportOperationRecord) for item in self.operations
        ):
            raise ExportProvenanceError("export operations must be immutable typed values")
        keys = tuple(_canonical(item.to_data()) for item in self.operations)
        if keys != tuple(sorted(set(keys))):
            raise ExportProvenanceError("export operations must be sorted and unique")
        if self.credential_kind != "unsigned-sidecar":
            raise ExportProvenanceError("I1 supports only explicit unsigned sidecars")

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifact": self.artifact.to_data(),
            "credential": {"kind": self.credential_kind},
            "document": {
                "documentId": self.document_id.value,
                "generationSha256": self.generation_digest.value,
                "manifestSha256": self.document_manifest_digest.value,
                "objectClosureSha256": self.object_closure_digest.value,
                "revisionId": self.revision.value,
            },
            "engineCompatibility": self.engine_compatibility.to_data(),
            "operations": [item.to_data() for item in self.operations],
            "presetSha256": self.preset_digest.value,
            "renderPlanSha256": self.render_plan_digest.value,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_data())

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())

    @classmethod
    def from_data(cls, value: object) -> ExportProvenance:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "artifact",
            "credential",
            "document",
            "engineCompatibility",
            "operations",
            "presetSha256",
            "renderPlanSha256",
        }:
            raise ExportProvenanceError("export sidecar has missing or unknown fields")
        document = value["document"]
        credential = value["credential"]
        operations = value["operations"]
        if not isinstance(document, dict) or set(document) != {
            "documentId",
            "generationSha256",
            "manifestSha256",
            "objectClosureSha256",
            "revisionId",
        }:
            raise ExportProvenanceError("sidecar document binding is malformed")
        if not isinstance(credential, dict) or set(credential) != {"kind"}:
            raise ExportProvenanceError("sidecar credential declaration is malformed")
        if not isinstance(operations, list):
            raise ExportProvenanceError("sidecar operation table is malformed")
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
                render_plan_digest=ObjectId.parse(value["renderPlanSha256"]),
                preset_digest=ObjectId.parse(value["presetSha256"]),
                engine_compatibility=EngineCompatibility.from_data(
                    value["engineCompatibility"]
                ),
                artifact=ExportArtifact.from_data(value["artifact"]),
                operations=tuple(
                    ExportOperationRecord.from_data(item) for item in operations
                ),
                credential_kind=credential["kind"],
            )
        except (TypeError, ValueError) as exc:
            raise ExportProvenanceError("export sidecar contains an invalid value") from exc

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        maximum_bytes: int,
    ) -> ExportProvenance:
        return cls.from_data(_parse_canonical(payload, maximum_bytes=maximum_bytes))

    def validate_join(self, preset: ExportPreset) -> None:
        if not isinstance(preset, ExportPreset):
            raise ExportProvenanceError("sidecar join requires a typed preset")
        if (
            self.document_id != preset.document_id
            or self.revision != preset.revision
            or self.document_manifest_digest != preset.document_manifest_digest
            or self.generation_digest != preset.generation_digest
            or self.object_closure_digest != preset.object_closure_digest
            or self.preset_digest != preset.digest
            or self.engine_compatibility.digest != preset.compatibility_digest
            or self.artifact.export_format is not preset.export_format
            or self.artifact.width != preset.width
            or self.artifact.height != preset.height
            or self.artifact.profile_digest != preset.output_profile
        ):
            raise ExportProvenanceError("sidecar does not join its preset and artifact")
        if (
            preset.metadata_policy is MetadataPolicy.STRIP
            and self.artifact.metadata_keys
        ):
            raise ExportProvenanceError("stripped export sidecar declares metadata")


def project_export_provenance(
    document: DocumentState,
    plan: RenderPlan,
    preset: ExportPreset,
    artifact: ExportArtifact,
    *,
    include_prompts: bool = False,
) -> ExportProvenance:
    """Project only operations that contribute to the visible render plan."""

    if not isinstance(document, DocumentState) or not isinstance(plan, RenderPlan):
        raise ExportProvenanceError("sidecar projection requires document and render plan")
    if not isinstance(preset, ExportPreset) or not isinstance(artifact, ExportArtifact):
        raise ExportProvenanceError("sidecar projection requires preset and artifact")
    if not isinstance(include_prompts, bool):
        raise ExportProvenanceError("prompt inclusion policy must be explicit")
    if (
        document.document_id != preset.document_id
        or document.revision_id != preset.revision
        or document.manifest_digest != preset.document_manifest_digest
        or object_closure_digest(document) != preset.object_closure_digest
        or document.engine_compatibility.digest != preset.compatibility_digest
        or plan.revision != preset.revision
        or plan.compatibility_digest != preset.compatibility_digest
        or plan.output_bounds != preset.crop
    ):
        raise ExportProvenanceError("document, plan and preset identities do not join")
    by_operation: dict[bytes, tuple[OperationProvenance, set[ObjectId]]] = {}
    visible = set(plan.layer_ids)
    for layer in document.layers:
        if layer.layer_id not in visible:
            continue
        operation = getattr(layer, "operation_provenance", None)
        if operation is not None:
            key = _canonical(operation.to_data())
            by_operation.setdefault(key, (operation, set()))
        mask = getattr(layer, "mask", None)
        if mask is not None and mask.operation_provenance is not None:
            key = _canonical(mask.operation_provenance.to_data())
            record = by_operation.setdefault(
                key,
                (mask.operation_provenance, set()),
            )
            record[1].add(mask.object_id)
    records = tuple(
        ExportOperationRecord(
            operation,
            include_prompts,
            tuple(sorted(masks, key=lambda item: item.value)),
        )
        for _, (operation, masks) in sorted(by_operation.items())
    )
    operations = tuple(sorted(records, key=lambda item: _canonical(item.to_data())))
    result = ExportProvenance(
        schema=EXPORT_PROVENANCE_SCHEMA,
        document_id=document.document_id,
        revision=document.revision_id,
        document_manifest_digest=document.manifest_digest,
        generation_digest=preset.generation_digest,
        object_closure_digest=preset.object_closure_digest,
        render_plan_digest=plan.digest,
        preset_digest=preset.digest,
        engine_compatibility=document.engine_compatibility,
        artifact=artifact,
        operations=operations,
    )
    result.validate_join(preset)
    return result


__all__ = (
    "EXPORT_PROVENANCE_SCHEMA",
    "ExportArtifact",
    "ExportOperationRecord",
    "ExportProvenance",
    "ExportProvenanceError",
    "project_export_provenance",
)
