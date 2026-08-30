"""Command verbs: every result is a view model, never a printed side effect."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import kilix_image_shop
from kilix_image_shop.domain.document import PROJECT_SCHEMA
from kilix_image_shop.domain.identifiers import DomainValidationError, ObjectId
from kilix_image_shop.engine import compatibility
from kilix_image_shop.export.presets import (
    EXPORT_PRESET_SCHEMA,
    ExportFormat,
    ExportPreset,
    ExportPresetError,
    deterministic_preset,
)
from kilix_image_shop.export.provenance import (
    EXPORT_PROVENANCE_SCHEMA,
    ExportProvenance,
    ExportProvenanceError,
)
from kilix_image_shop.ops.diagnostics import diagnostic_catalogue
from kilix_image_shop.ops.messages import OperationKind
from kilix_image_shop.ops.orchestrator import OperationOrchestrator
from kilix_image_shop.store.gc import apply_gc, preview_gc
from kilix_image_shop.store.generations import read_head
from kilix_image_shop.store.layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    read_regular_file,
    write_new_file,
)
from kilix_image_shop.store.objects import ObjectStore
from kilix_image_shop.store.recovery import (
    OpenedProject,
    apply_recovery,
    list_recovery_candidates,
    open_project,
    preview_recovery,
)

from .configuration import (
    DEFAULT_MAX_ACTIVE_OPERATIONS,
    DEFAULT_MAX_PRESET_BYTES,
    DEFAULT_MAX_RETAINED_OPERATIONS,
    DEFAULT_MAX_SIDECAR_BYTES,
    ExitCode,
    limit_data,
    limit_rows,
)
from . import environment
from .presentation import Report, counted


class CommandError(RuntimeError):
    """One command failed with an exact exit status and a local message."""

    def __init__(self, message: str, exit_code: ExitCode) -> None:
        if not isinstance(exit_code, ExitCode):
            raise ValueError("command failure requires a closed exit status")
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class Outcome:
    report: Report
    exit_code: ExitCode

    def __post_init__(self) -> None:
        if not isinstance(self.report, Report) or not isinstance(
            self.exit_code, ExitCode
        ):
            raise ValueError("command outcome must carry a report and an exit status")


# Exactly the typed refusals the core raises for hostile or drifted data. A
# bare ValueError is deliberately absent: it would hide an internal defect
# behind a data-shaped exit status.
_DATA_ERRORS = (
    StoreError,
    DomainValidationError,
    ExportPresetError,
    ExportProvenanceError,
)


def _resolved_directory(value: str, label: str) -> pathlib.Path:
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CommandError(f"{label} cannot be resolved", ExitCode.INVALID_DATA) from exc
    if not path.is_dir():
        raise CommandError(f"{label} is not a directory", ExitCode.INVALID_DATA)
    return path


def _resolved_file(value: str, label: str) -> pathlib.Path:
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CommandError(f"{label} cannot be resolved", ExitCode.INVALID_DATA) from exc
    if not path.is_file():
        raise CommandError(f"{label} is not a regular file", ExitCode.INVALID_DATA)
    return path


def _bounded_bytes(path: pathlib.Path, label: str, maximum_bytes: int) -> bytes:
    try:
        return read_regular_file(path, maximum_bytes=maximum_bytes)
    except StoreError as exc:
        raise CommandError(f"{label} cannot be read", ExitCode.INVALID_DATA) from exc


def _object_id(value: str, label: str) -> ObjectId:
    try:
        return ObjectId.parse(value)
    except (TypeError, ValueError) as exc:
        raise CommandError(
            f"{label} is not a content identity",
            ExitCode.USAGE,
        ) from exc


def _opened(root: pathlib.Path, limits: ProjectLimits) -> OpenedProject:
    try:
        return open_project(ProjectLayout(root), limits)
    except _DATA_ERRORS as exc:
        raise CommandError(f"project cannot be opened: {exc}", ExitCode.INVALID_DATA) from exc


def version_command() -> Outcome:
    """Report product, schema and accepted engine-group identity."""

    rows = (
        ("product", "kilix-image-shop"),
        ("version", kilix_image_shop.__version__),
        ("schema.project", PROJECT_SCHEMA),
        ("schema.exportPreset", EXPORT_PRESET_SCHEMA),
        ("schema.exportProvenance", EXPORT_PROVENANCE_SCHEMA),
        ("engine.packageGroup", compatibility.PACKAGE_GROUP_ID),
        ("engine.gegl", compatibility.GEGL_PACKAGE_VERSION),
        ("engine.babl", compatibility.BABL_PACKAGE_VERSION),
        ("engine.pythonGi", compatibility.PYTHON_GI_PACKAGE_VERSION),
        ("providers", counted(0, len(OperationKind))),
    )
    data = {
        "product": "kilix-image-shop",
        "version": kilix_image_shop.__version__,
        "schemas": {
            "project": PROJECT_SCHEMA,
            "exportPreset": EXPORT_PRESET_SCHEMA,
            "exportProvenance": EXPORT_PROVENANCE_SCHEMA,
        },
        "engine": {
            "packageGroup": compatibility.PACKAGE_GROUP_ID,
            "gegl": compatibility.GEGL_PACKAGE_VERSION,
            "babl": compatibility.BABL_PACKAGE_VERSION,
            "pythonGi": compatibility.PYTHON_GI_PACKAGE_VERSION,
        },
        "providers": {"installed": 0, "declared": len(OperationKind)},
    }
    return Outcome(Report("version", rows, data), ExitCode.OK)


def doctor_command(
    *,
    status_path: pathlib.Path | None = None,
    environ: dict[str, str] | None = None,
    isolated: int | None = None,
    gi_origin: pathlib.Path | None = None,
) -> Outcome:
    """Probe installed components without starting the engine or touching a project."""

    keywords: dict[str, object] = {}
    if status_path is not None:
        keywords["status_path"] = status_path
    if gi_origin is not None:
        keywords["gi_origin"] = gi_origin
    report = environment.readiness(
        environ=environ,
        isolated=isolated,
        **keywords,
    )
    rows = [
        (item.component, f"{item.state.value} (expected {item.expected}; observed {item.observed})")
        for item in report.components
    ]
    rows.append(
        (
            "required.ready",
            counted(report.required_ready, report.required_total),
        )
    )
    rows.append(
        (
            "conventionalEditing",
            "ready" if report.conventional_editing_ready else "unavailable",
        )
    )
    exit_code = (
        ExitCode.OK if report.conventional_editing_ready else ExitCode.UNAVAILABLE
    )
    return Outcome(Report("doctor", tuple(rows), report.to_data()), exit_code)


def _document_rows(opened: OpenedProject) -> tuple[tuple[str, str], ...]:
    document = opened.generation.document
    return (
        ("documentId", document.document_id.value),
        ("revisionId", document.revision_id.value),
        ("headGeneration", opened.generation.generation_id.value),
        ("manifestSha256", document.manifest_digest.value),
        ("canvas", f"{document.canvas.width}x{document.canvas.height}"),
        ("workingFormat", document.engine_compatibility.working_format),
        ("engineCompatibilitySha256", document.engine_compatibility.digest.value),
        ("layers", counted(len(document.layers), len(document.layers))),
        ("rootLayers", counted(len(document.root_layer_ids), len(document.layers))),
        ("assets", counted(len(document.assets), len(document.assets))),
        (
            "provenanceRecords",
            counted(len(document.provenance), len(document.provenance)),
        ),
        (
            "validationClasses",
            counted(len(opened.validated_classes), len(opened.validated_classes)),
        ),
    )


def _document_data(opened: OpenedProject) -> dict[str, object]:
    document = opened.generation.document
    return {
        "documentId": document.document_id.value,
        "revisionId": document.revision_id.value,
        "headGeneration": opened.generation.generation_id.value,
        "manifestSha256": document.manifest_digest.value,
        "canvas": {
            "width": document.canvas.width,
            "height": document.canvas.height,
        },
        "workingFormat": document.engine_compatibility.working_format,
        "engineCompatibilitySha256": document.engine_compatibility.digest.value,
        "layerCount": len(document.layers),
        "rootLayerCount": len(document.root_layer_ids),
        "assetCount": len(document.assets),
        "provenanceCount": len(document.provenance),
        "validatedClasses": list(opened.validated_classes),
    }


def project_info_command(root_argument: str, limits: ProjectLimits) -> Outcome:
    """Open one project through all 10/10 validation classes and describe it."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    closure = opened.generation.objects
    rows = _document_rows(opened) + (
        ("closureObjects", counted(len(closure), len(closure))),
        ("closureBytes", str(sum(item.byte_count for item in closure))),
    ) + limit_rows(limits)
    data = _document_data(opened)
    data["closure"] = {
        "objectCount": len(closure),
        "byteCount": sum(item.byte_count for item in closure),
    }
    data["limits"] = limit_data(limits)
    return Outcome(Report("project.info", rows, data), ExitCode.OK)


def project_verify_command(root_argument: str, limits: ProjectLimits) -> Outcome:
    """Re-read and re-digest every object the current generation depends on."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    layout = ProjectLayout(root)
    store = ObjectStore(layout, limits)
    closure = opened.generation.objects
    verified = 0
    failures: list[str] = []
    for record in closure:
        try:
            store.verify(record)
        except StoreError as exc:
            failures.append(f"{record.object_id.value}: {exc}")
        else:
            verified += 1
    try:
        stored = store.enumerate_records()
    except StoreError as exc:
        raise CommandError(f"object store cannot be enumerated: {exc}", ExitCode.INVALID_DATA) from exc
    rows = (
        ("documentId", opened.generation.document.document_id.value),
        ("headGeneration", opened.generation.generation_id.value),
        ("closureVerified", counted(verified, len(closure))),
        ("storedObjects", counted(len(stored), len(stored))),
        ("storedBytes", str(sum(item.byte_count for item in stored))),
        ("failures", counted(len(failures), len(closure))),
    )
    data = {
        "documentId": opened.generation.document.document_id.value,
        "headGeneration": opened.generation.generation_id.value,
        "closureObjectCount": len(closure),
        "closureVerifiedCount": verified,
        "storedObjectCount": len(stored),
        "storedByteCount": sum(item.byte_count for item in stored),
        "failures": failures,
    }
    exit_code = ExitCode.OK if not failures else ExitCode.INVALID_DATA
    return Outcome(Report("project.verify", rows, data), exit_code)


def project_generations_command(root_argument: str, limits: ProjectLimits) -> Outcome:
    """List retained generations and mark the one HEAD currently selects."""

    root = _resolved_directory(root_argument, "project root")
    layout = ProjectLayout(root)
    try:
        layout.verify_structure()
        candidates = list_recovery_candidates(layout)
        head = read_head(layout, allow_missing=True)
    except _DATA_ERRORS as exc:
        raise CommandError(f"project cannot be read: {exc}", ExitCode.INVALID_DATA) from exc
    rows = tuple(
        (
            "generation",
            f"{item.value} {'head' if head is not None and item == head else 'retained'}",
        )
        for item in candidates
    ) + (
        ("head", head.value if head is not None else "absent"),
        ("generations", counted(len(candidates), len(candidates))),
    )
    data = {
        "head": head.value if head is not None else None,
        "generations": [
            {
                "generation": item.value,
                "isHead": head is not None and item == head,
            }
            for item in candidates
        ],
    }
    return Outcome(Report("project.generations", rows, data), ExitCode.OK)


def project_recover_command(
    root_argument: str,
    generation_argument: str,
    *,
    apply: bool,
    limits: ProjectLimits,
) -> Outcome:
    """Preview one named generation; select it only under an explicit apply."""

    root = _resolved_directory(root_argument, "project root")
    candidate_id = _object_id(generation_argument, "generation identity")
    layout = ProjectLayout(root)
    try:
        preview = preview_recovery(layout, candidate_id, limits)
    except _DATA_ERRORS as exc:
        raise CommandError(
            f"recovery candidate is not selectable: {exc}",
            ExitCode.INVALID_DATA,
        ) from exc
    applied = False
    if apply:
        try:
            apply_recovery(preview, limits)
        except _DATA_ERRORS as exc:
            raise CommandError(
                f"recovery could not be applied: {exc}",
                ExitCode.INVALID_DATA,
            ) from exc
        applied = True
    closure = preview.candidate.objects
    rows = (
        ("candidateGeneration", preview.candidate.generation_id.value),
        ("candidateRevision", preview.candidate.document.revision_id.value),
        ("candidateObjects", counted(len(closure), len(closure))),
        ("originalHeadSha256", preview.original_head_sha256.value),
        ("mode", "applied" if applied else "preview"),
        ("headReplaced", counted(1 if applied else 0, 1)),
    )
    data = {
        "candidateGeneration": preview.candidate.generation_id.value,
        "candidateRevision": preview.candidate.document.revision_id.value,
        "candidateObjectCount": len(closure),
        "originalHeadSha256": preview.original_head_sha256.value,
        "applied": applied,
    }
    return Outcome(Report("project.recover", rows, data), ExitCode.OK)


def project_gc_command(
    root_argument: str,
    *,
    apply: bool,
    limits: ProjectLimits,
) -> Outcome:
    """Compute one exact reachability collection; quarantine only under apply."""

    root = _resolved_directory(root_argument, "project root")
    layout = ProjectLayout(root)
    try:
        preview = preview_gc(layout, limits)
        retained_total = len(list_recovery_candidates(layout))
    except _DATA_ERRORS as exc:
        raise CommandError(
            f"collection cannot be previewed: {exc}",
            ExitCode.INVALID_DATA,
        ) from exc
    quarantine: str | None = None
    moved = 0
    if apply:
        try:
            result = apply_gc(preview, limits)
        except _DATA_ERRORS as exc:
            raise CommandError(
                f"collection could not be applied: {exc}",
                ExitCode.INVALID_DATA,
            ) from exc
        moved = len(result.moved_objects)
        quarantine = None if result.quarantine is None else result.quarantine.name
    unreachable = preview.unreachable_objects
    rows = (
        ("rootGenerations", counted(len(preview.retained_generations), retained_total)),
        ("reachableObjects", counted(len(preview.reachable_objects), len(preview.reachable_objects))),
        ("unreachableObjects", counted(len(unreachable), len(preview.reachable_objects) + len(unreachable))),
        ("unreachableBytes", str(sum(item.byte_count for item in unreachable))),
        ("mode", "applied" if apply else "preview"),
        ("quarantinedObjects", counted(moved, len(unreachable))),
        ("quarantine", quarantine if quarantine is not None else "none"),
    )
    data = {
        "rootGenerationCount": len(preview.retained_generations),
        "retainedGenerationCount": retained_total,
        "reachableObjectCount": len(preview.reachable_objects),
        "unreachableObjectCount": len(unreachable),
        "unreachableByteCount": sum(item.byte_count for item in unreachable),
        "applied": apply,
        "quarantinedObjectCount": moved,
        "quarantine": quarantine,
    }
    return Outcome(Report("project.gc", rows, data), ExitCode.OK)


def ops_providers_command() -> Outcome:
    """Report the production operation registry exactly as I1 ships it: 0 providers."""

    orchestrator = OperationOrchestrator.zero_provider(
        max_active_requests=DEFAULT_MAX_ACTIVE_OPERATIONS,
        max_retained_requests=DEFAULT_MAX_RETAINED_OPERATIONS,
    )
    views = sorted(
        orchestrator.availability_views(),
        key=lambda item: item.operation.value,
    )
    available = sum(item.available for item in views)
    rows = tuple(
        (
            f"operation.{item.operation.value}",
            "available" if item.available else "unavailable",
        )
        for item in views
    ) + (
        ("providersInstalled", counted(orchestrator.provider_count, len(OperationKind))),
        ("availableOperations", counted(available, len(views))),
    )
    data = {
        "providerCount": orchestrator.provider_count,
        "declaredOperationCount": len(OperationKind),
        "operations": [
            {
                "operation": item.operation.value,
                "available": item.available,
                "providerId": item.provider_id,
            }
            for item in views
        ],
    }
    return Outcome(Report("ops.providers", rows, data), ExitCode.OK)


def ops_diagnostics_command() -> Outcome:
    """Print the closed local diagnostic catalogue; provider prose has no channel."""

    catalogue = diagnostic_catalogue()
    rows = tuple((f"{code.value}", f"{message_id}: {text}") for code, message_id, text in catalogue)
    rows = rows + (("messages", counted(len(catalogue), len(catalogue))),)
    data = {
        "messages": [
            {"code": code.value, "messageId": message_id, "text": text}
            for code, message_id, text in catalogue
        ]
    }
    return Outcome(Report("ops.diagnostics", rows, data), ExitCode.OK)


def export_preset_command(
    root_argument: str,
    format_argument: str,
    *,
    output_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Bind the current generation to one deterministic preset without rendering."""

    try:
        export_format = ExportFormat(format_argument)
    except ValueError as exc:
        raise CommandError("export format is outside the closed set", ExitCode.USAGE) from exc
    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        preset = deterministic_preset(
            opened.generation.document,
            opened.generation.generation_id,
            export_format,
        )
    except _DATA_ERRORS as exc:
        raise CommandError(f"preset cannot be bound: {exc}", ExitCode.INVALID_DATA) from exc
    written: str | None = None
    if output_argument is not None:
        destination = pathlib.Path(output_argument).expanduser()
        if not destination.is_absolute():
            destination = pathlib.Path.cwd() / destination
        try:
            write_new_file(destination, preset.canonical_bytes())
        except StoreError as exc:
            raise CommandError(
                f"preset carrier cannot be written: {exc}",
                ExitCode.INVALID_DATA,
            ) from exc
        written = destination.name
    rows = (
        ("documentId", preset.document_id.value),
        ("generationSha256", preset.generation_digest.value),
        ("presetSha256", preset.digest.value),
        ("format", preset.export_format.value),
        ("geometry", f"{preset.width}x{preset.height}"),
        ("workingFormat", preset.working_format.value),
        ("alphaPolicy", preset.alpha_policy.value),
        ("metadataPolicy", preset.metadata_policy.value),
        (
            "determinismBindings",
            counted(len(preset.binding_groups), len(preset.binding_groups)),
        ),
        ("carrier", written if written is not None else "stdout-only"),
        ("renderedPixels", counted(0, 1)),
    )
    data = {
        "documentId": preset.document_id.value,
        "generationSha256": preset.generation_digest.value,
        "presetSha256": preset.digest.value,
        "format": preset.export_format.value,
        "width": preset.width,
        "height": preset.height,
        "workingFormat": preset.working_format.value,
        "alphaPolicy": preset.alpha_policy.value,
        "metadataPolicy": preset.metadata_policy.value,
        "determinismBindings": list(preset.binding_groups),
        "carrier": written,
        "renderedPixels": 0,
    }
    return Outcome(Report("export.preset", rows, data), ExitCode.OK)


def export_verify_command(
    sidecar_argument: str,
    preset_argument: str,
    *,
    artifact_argument: str | None,
) -> Outcome:
    """Verify a sidecar against its preset, and optionally against real bytes."""

    sidecar_path = _resolved_file(sidecar_argument, "sidecar")
    preset_path = _resolved_file(preset_argument, "preset")
    sidecar_bytes = _bounded_bytes(sidecar_path, "sidecar", DEFAULT_MAX_SIDECAR_BYTES)
    preset_bytes = _bounded_bytes(preset_path, "preset", DEFAULT_MAX_PRESET_BYTES)
    try:
        provenance = ExportProvenance.from_bytes(
            sidecar_bytes,
            maximum_bytes=DEFAULT_MAX_SIDECAR_BYTES,
        )
        preset = ExportPreset.from_bytes(preset_bytes)
    except _DATA_ERRORS as exc:
        raise CommandError(f"carrier is invalid: {exc}", ExitCode.INVALID_DATA) from exc
    try:
        provenance.validate_join(preset)
    except ExportProvenanceError as exc:
        raise CommandError(f"sidecar does not join its preset: {exc}", ExitCode.INVALID_DATA) from exc
    checks = 2
    artifact_checked = 0
    artifact_name: str | None = None
    if artifact_argument is not None:
        artifact_path = _resolved_file(artifact_argument, "artifact")
        try:
            payload = artifact_path.read_bytes()
        except OSError as exc:
            raise CommandError("artifact cannot be read", ExitCode.INVALID_DATA) from exc
        if len(payload) != provenance.artifact.byte_count:
            raise CommandError(
                "artifact byte count differs from its sidecar",
                ExitCode.INVALID_DATA,
            )
        if ObjectId.from_bytes(payload) != provenance.artifact.image_digest:
            raise CommandError(
                "artifact digest differs from its sidecar",
                ExitCode.INVALID_DATA,
            )
        artifact_checked = 1
        artifact_name = artifact_path.name
        checks += 2
    rows = (
        ("documentId", provenance.document_id.value),
        ("revisionId", provenance.revision.value),
        ("presetSha256", provenance.preset_digest.value),
        ("artifactSha256", provenance.artifact.image_digest.value),
        ("artifactBytes", str(provenance.artifact.byte_count)),
        ("format", provenance.artifact.export_format.value),
        ("credential", provenance.credential_kind),
        (
            "operations",
            counted(len(provenance.operations), len(provenance.operations)),
        ),
        ("artifactBytesChecked", counted(artifact_checked, 1)),
        ("checks", counted(checks, checks)),
        ("artifact", artifact_name if artifact_name is not None else "not supplied"),
    )
    data = {
        "documentId": provenance.document_id.value,
        "revisionId": provenance.revision.value,
        "presetSha256": provenance.preset_digest.value,
        "artifactSha256": provenance.artifact.image_digest.value,
        "artifactByteCount": provenance.artifact.byte_count,
        "format": provenance.artifact.export_format.value,
        "credentialKind": provenance.credential_kind,
        "operationCount": len(provenance.operations),
        "artifactBytesChecked": bool(artifact_checked),
        "checkCount": checks,
    }
    return Outcome(Report("export.verify", rows, data), ExitCode.OK)


__all__ = (
    "CommandError",
    "Outcome",
    "doctor_command",
    "export_preset_command",
    "export_verify_command",
    "ops_diagnostics_command",
    "ops_providers_command",
    "project_gc_command",
    "project_generations_command",
    "project_info_command",
    "project_recover_command",
    "project_verify_command",
    "version_command",
)
