"""Command verbs: every result is a view model, never a printed side effect."""

from __future__ import annotations

import json
import pathlib
import uuid
from dataclasses import dataclass

import kilix_image_shop
from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.color import ColourSpace, ColourState, EngineCompatibility
from kilix_image_shop.domain.commands import (
    AddLayer,
    AttachMask,
    ChangeAdjustment,
    CropCanvas,
    FlattenLayers,
    ImportAsset,
    RemoveLayer,
    ReorderLayer,
    ReductionContext,
    ResolvedObject,
    SetLayerProperty,
    SetSelection,
    SetTransform,
    reduce_command,
)
from kilix_image_shop.domain.document import PROJECT_SCHEMA, DocumentState
from kilix_image_shop.domain.geometry import AffineTransform, Canvas, Rect
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    DomainValidationError,
    LayerId,
    ObjectId,
    RevisionId,
)
from kilix_image_shop.domain.layers import (
    AdjustmentId,
    AdjustmentLayer,
    BlendMode,
    FontAxis,
    FontFallback,
    GroupLayer,
    MaskObject,
    MaskSource,
    PixelLayer,
    Selection,
    SelectionKind,
    TextAlignment,
    TextLayer,
    TextLayout,
)
from kilix_image_shop.editing.adjustments import make_adjustment
from kilix_image_shop.editing.masking import paint_mask
from kilix_image_shop.editing.selection import selection_to_mask
from kilix_image_shop.editing.text import (
    EditableText,
    TextValidationError,
    add_text_layer,
    edit_text_layer,
    font_digest,
)
from kilix_image_shop.engine import compatibility
from kilix_image_shop.engine.api import mask_digest_index
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
from kilix_image_shop.store.generations import (
    GenerationStore,
    create_project,
    read_head,
)
from kilix_image_shop.store.layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    parse_canonical_json,
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


def _document_id(value: str, label: str = "document identity") -> DocumentId:
    try:
        return DocumentId.parse(value)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"{label} is not a canonical UUID", ExitCode.USAGE) from exc


def _revision_id(value: str | None) -> RevisionId:
    if value is None:
        return RevisionId(str(uuid.uuid4()))
    try:
        return RevisionId.parse(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("revision identity is not a canonical UUID", ExitCode.USAGE) from exc


def _layer_id(value: str | None, label: str = "layer identity") -> LayerId:
    if value is None:
        return LayerId(str(uuid.uuid4()))
    try:
        return LayerId.parse(value)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"{label} is not a canonical UUID", ExitCode.USAGE) from exc


def _optional_layer_id(value: str | None, label: str) -> LayerId | None:
    return None if value is None else _layer_id(value, label)


def _new_project_root(value: str) -> pathlib.Path:
    candidate = pathlib.Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = pathlib.Path.cwd() / candidate
    if not candidate.name or candidate.name in {".", ".."}:
        raise CommandError("project root basename is invalid", ExitCode.USAGE)
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise CommandError("project parent cannot be resolved", ExitCode.INVALID_DATA) from exc
    if not parent.is_dir():
        raise CommandError("project parent is not a directory", ExitCode.INVALID_DATA)
    root = parent / candidate.name
    try:
        root.lstat()
    except FileNotFoundError:
        return root
    except OSError as exc:
        raise CommandError("project destination cannot be inspected", ExitCode.INVALID_DATA) from exc
    raise CommandError("project destination already exists", ExitCode.INVALID_DATA)


def _engine_compatibility(path_argument: str, limits: ProjectLimits) -> EngineCompatibility:
    path = _resolved_file(path_argument, "engine compatibility carrier")
    payload = _bounded_bytes(
        path,
        "engine compatibility carrier",
        limits.max_manifest_bytes,
    )
    try:
        value = parse_canonical_json(payload, maximum_bytes=limits.max_manifest_bytes)
        compatibility_value = EngineCompatibility.from_data(value)
    except _DATA_ERRORS as exc:
        raise CommandError(
            f"engine compatibility carrier is invalid: {exc}",
            ExitCode.INVALID_DATA,
        ) from exc
    if compatibility_value.canonical_bytes() != payload:
        raise CommandError(
            "engine compatibility carrier is not canonical",
            ExitCode.INVALID_DATA,
        )
    return compatibility_value


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _parameter_map(arguments: tuple[str, ...]) -> dict[str, object]:
    values: dict[str, object] = {}
    for argument in arguments:
        name, separator, raw = argument.partition("=")
        if not separator or not name or name in values:
            raise CommandError(
                "adjustment parameters must be unique NAME=JSON pairs",
                ExitCode.USAGE,
            )
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CommandError(
                f"adjustment parameter {name!r} is not strict JSON",
                ExitCode.USAGE,
            ) from exc
        if isinstance(value, list):
            value = tuple(value)
        if isinstance(value, (dict, type(None))):
            raise CommandError(
                f"adjustment parameter {name!r} has an unsupported JSON type",
                ExitCode.USAGE,
            )
        values[name] = value
    return values


def _font_axes(arguments: tuple[str, ...]) -> tuple[FontAxis, ...]:
    values: dict[str, FontAxis] = {}
    for argument in arguments:
        tag, separator, raw = argument.partition("=")
        if not separator or not tag or tag in values:
            raise CommandError(
                "font axes must be unique TAG=NUMBER pairs",
                ExitCode.USAGE,
            )
        try:
            values[tag] = FontAxis(tag, float(raw))
        except (TypeError, ValueError) as exc:
            raise CommandError(
                f"font axis {tag!r} is invalid: {exc}",
                ExitCode.USAGE,
            ) from exc
    return tuple(sorted(values.values(), key=lambda item: item.tag))


def _editable_text(
    font_payload: bytes,
    *,
    text: str,
    width: int,
    height: int,
    alignment_argument: str,
    language: str,
    face_index: int,
    axis_arguments: tuple[str, ...],
    preview_argument: str,
    fallbacks: tuple[FontFallback, ...] = (),
) -> EditableText:
    if not font_payload:
        raise CommandError("font carrier is empty", ExitCode.INVALID_DATA)
    try:
        return EditableText(
            text=text,
            layout=TextLayout(
                width,
                height,
                TextAlignment(alignment_argument),
                language,
            ),
            font_digest=font_digest(font_payload),
            face_index=face_index,
            axes=_font_axes(axis_arguments),
            fallbacks=fallbacks,
            preview_asset_digest=_object_id(
                preview_argument,
                "text preview asset identity",
            ),
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"editable text arguments are invalid: {exc}", ExitCode.USAGE) from exc


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


def project_create_command(
    root_argument: str,
    compatibility_argument: str,
    *,
    width: int,
    height: int,
    document_id_argument: str | None,
    revision_id_argument: str | None,
    declared_space_argument: str,
    limits: ProjectLimits,
) -> Outcome:
    """Create one empty project through the complete atomic save transaction."""

    root = _new_project_root(root_argument)
    compatibility_value = _engine_compatibility(compatibility_argument, limits)
    document_id = (
        DocumentId(str(uuid.uuid4()))
        if document_id_argument is None
        else _document_id(document_id_argument)
    )
    revision_id = _revision_id(revision_id_argument)
    try:
        declared_space = ColourSpace(declared_space_argument)
        document = DocumentState(
            schema=PROJECT_SCHEMA,
            document_id=document_id,
            revision_id=revision_id,
            canvas=Canvas(width, height),
            colour=ColourState(
                working_profile=compatibility_value.working_profile,
                declared_space=declared_space,
                conversion_policy=compatibility_value.conversion_policy,
            ),
            engine_compatibility=compatibility_value,
            assets=(),
            root_layer_ids=(),
            layers=(),
        )
        layout, generation = create_project(
            root,
            document,
            limits=limits,
            object_payloads={},
        )
        opened = open_project(layout, limits)
    except _DATA_ERRORS as exc:
        raise CommandError(f"project cannot be created: {exc}", ExitCode.INVALID_DATA) from exc
    if opened.generation != generation or opened.generation.document != document:
        raise CommandError("project readback differs after creation", ExitCode.INTERNAL)
    rows = (
        ("documentId", document.document_id.value),
        ("revisionId", document.revision_id.value),
        ("headGeneration", generation.generation_id.value),
        ("canvas", f"{document.canvas.width}x{document.canvas.height}"),
        ("layers", counted(0, 0)),
        ("saveTransaction", counted(12, 12)),
        ("validationClasses", counted(len(opened.validated_classes), 10)),
        ("readback", counted(1, 1)),
    )
    data = {
        "documentId": document.document_id.value,
        "revisionId": document.revision_id.value,
        "headGeneration": generation.generation_id.value,
        "width": document.canvas.width,
        "height": document.canvas.height,
        "layerCount": 0,
        "savePointCount": 12,
        "validationClassCount": len(opened.validated_classes),
        "readbackVerified": True,
    }
    return Outcome(Report("project.create", rows, data), ExitCode.OK)


def project_layers_command(root_argument: str, limits: ProjectLimits) -> Outcome:
    """List the exact immutable layer population selected by HEAD."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    document = opened.generation.document
    parents: dict[LayerId, LayerId | None] = {
        layer_id: None for layer_id in document.root_layer_ids
    }
    for layer in document.layers:
        if isinstance(layer, GroupLayer):
            for child in layer.child_layer_ids:
                parents[child] = layer.layer_id
    ordered: list[tuple[object, LayerId | None, int]] = []

    def walk(layer_id: LayerId, depth: int) -> None:
        layer = document.layer_map[layer_id]
        ordered.append((layer, parents[layer_id], depth))
        if isinstance(layer, GroupLayer):
            for child in layer.child_layer_ids:
                walk(child, depth + 1)

    for root_id in document.root_layer_ids:
        walk(root_id, 0)
    rows = tuple(
        (
            f"layer.{index + 1}",
            f"{layer.layer_id.value} {type(layer).__name__} depth={depth} name={layer.name!r}",
        )
        for index, (layer, _, depth) in enumerate(ordered)
    ) + (
        ("headGeneration", opened.generation.generation_id.value),
        ("layers", counted(len(ordered), len(document.layers))),
    )
    data = {
        "headGeneration": opened.generation.generation_id.value,
        "layers": [
            {
                "layerId": layer.layer_id.value,
                "kind": {
                    PixelLayer: "pixel",
                    AdjustmentLayer: "adjustment",
                    TextLayer: "text",
                    GroupLayer: "group",
                }[type(layer)],
                "name": layer.name,
                "parentId": None if parent is None else parent.value,
                "depth": depth,
                "visible": layer.visible,
                "opacityU16": layer.opacity_u16,
                "blendMode": layer.blend_mode.value,
                "hasMask": getattr(layer, "mask", None) is not None,
            }
            for layer, parent, depth in ordered
        ],
        "layerCount": len(ordered),
    }
    return Outcome(Report("project.layers", rows, data), ExitCode.OK)


def _commit_edit(
    root_argument: str,
    limits: ProjectLimits,
    command: object,
    *,
    payloads: dict[ObjectId, bytes],
    report_name: str,
    resolved_objects: tuple[ResolvedObject, ...] = (),
    detail_rows: tuple[tuple[str, str], ...] = (),
    detail_data: dict[str, object] | None = None,
) -> Outcome:
    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    before = opened.generation
    resolved = {
        object_id: ResolvedObject(object_id, len(payload))
        for object_id, payload in payloads.items()
    }
    for item in resolved_objects:
        present = resolved.get(item.object_id)
        if present is not None and present.byte_count != item.byte_count:
            raise CommandError(
                "resolved object metadata disagrees with its supplied payload",
                ExitCode.INTERNAL,
            )
        resolved[item.object_id] = item
    context = ReductionContext(
        tuple(sorted(resolved.values(), key=lambda item: item.object_id.value))
    )
    try:
        reduction = reduce_command(before.document, command, context)
        generation = GenerationStore(ProjectLayout(root), limits).save(
            reduction.state,
            object_payloads=payloads,
            expected_head=before.generation_id,
        )
        readback = open_project(ProjectLayout(root), limits)
    except _DATA_ERRORS as exc:
        raise CommandError(f"edit cannot be committed: {exc}", ExitCode.INVALID_DATA) from exc
    if readback.generation != generation or readback.generation.document != reduction.state:
        raise CommandError("edit readback differs after commit", ExitCode.INTERNAL)
    rows = (
        ("documentId", reduction.state.document_id.value),
        ("beforeRevision", reduction.before_revision.value),
        ("revisionId", reduction.state.revision_id.value),
        ("headGeneration", generation.generation_id.value),
        (
            "changedLayers",
            counted(len(reduction.changed_layer_ids), len(reduction.changed_layer_ids)),
        ),
        ("objectPayloadsAccepted", counted(len(payloads), len(payloads))),
        ("saveTransaction", counted(12, 12)),
        ("validationClasses", counted(len(readback.validated_classes), 10)),
        ("documentMutation", counted(1, 1)),
        ("readback", counted(1, 1)),
    ) + detail_rows
    data = {
        "documentId": reduction.state.document_id.value,
        "beforeRevision": reduction.before_revision.value,
        "revisionId": reduction.state.revision_id.value,
        "headGeneration": generation.generation_id.value,
        "changedLayerIds": [item.value for item in reduction.changed_layer_ids],
        "acceptedObjectPayloadCount": len(payloads),
        "savePointCount": 12,
        "validationClassCount": len(readback.validated_classes),
        "documentMutated": True,
        "readbackVerified": True,
    }
    if detail_data is not None:
        data.update(detail_data)
    return Outcome(Report(report_name, rows, data), ExitCode.OK)


def edit_import_command(
    root_argument: str,
    asset_argument: str,
    *,
    media_type_argument: str,
    width: int,
    height: int,
    profile_argument: str,
    name: str,
    layer_id_argument: str | None,
    revision_id_argument: str | None,
    parent_id_argument: str | None,
    index: int,
    limits: ProjectLimits,
) -> Outcome:
    """Copy one bounded encoded image carrier into a new pixel layer."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    source = _resolved_file(asset_argument, "asset")
    payload = _bounded_bytes(source, "asset", limits.max_object_bytes)
    identity = ObjectId.from_bytes(payload)
    try:
        media_type = MediaType(media_type_argument)
        profile = ObjectId.parse(profile_argument)
        layer_id = _layer_id(layer_id_argument)
        revision_id = _revision_id(revision_id_argument)
        parent_id = _optional_layer_id(parent_id_argument, "parent layer identity")
        asset = AssetRef(
            digest=identity,
            byte_count=len(payload),
            media_type=media_type,
            width=width,
            height=height,
            profile_digest=profile,
            import_policy=ImportPolicy.COPIED,
        )
        layer = PixelLayer(layer_id=layer_id, name=name, asset_digest=identity)
        command = ImportAsset(
            expected_revision=opened.generation.document.revision_id,
            new_revision=revision_id,
            asset=asset,
            layer=layer,
            parent_id=parent_id,
            index=index,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"asset import arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={identity: payload},
        report_name="edit.import",
    )


def edit_adjustment_command(
    root_argument: str,
    adjustment_argument: str,
    *,
    parameter_arguments: tuple[str, ...],
    name: str,
    layer_id_argument: str | None,
    revision_id_argument: str | None,
    parent_id_argument: str | None,
    index: int,
    limits: ProjectLimits,
) -> Outcome:
    """Add one validated non-destructive adjustment layer."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        adjustment = make_adjustment(
            AdjustmentId(adjustment_argument),
            _parameter_map(parameter_arguments),
        )
        layer = AdjustmentLayer(
            layer_id=_layer_id(layer_id_argument),
            name=name,
            adjustment=adjustment,
        )
        command = AddLayer(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer=layer,
            parent_id=_optional_layer_id(parent_id_argument, "parent layer identity"),
            index=index,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"adjustment arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.adjustment",
    )


def edit_mask_command(
    root_argument: str,
    layer_id_argument: str,
    mask_argument: str,
    *,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Attach or replace one full-canvas editable foreground-alpha mask."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    document = opened.generation.document
    source = _resolved_file(mask_argument, "mask")
    expected_bytes = document.canvas.width * document.canvas.height
    if expected_bytes > limits.max_object_bytes:
        raise CommandError("mask exceeds the object byte ceiling", ExitCode.INVALID_DATA)
    payload = _bounded_bytes(source, "mask", expected_bytes)
    if len(payload) != expected_bytes:
        raise CommandError(
            "mask bytes differ from the full-canvas Y u8 geometry",
            ExitCode.INVALID_DATA,
        )
    identity = ObjectId.from_bytes(payload)
    try:
        layer_id = _layer_id(layer_id_argument)
        mask = MaskObject(
            object_id=identity,
            width=document.canvas.width,
            height=document.canvas.height,
            origin_x=document.canvas.origin_x,
            origin_y=document.canvas.origin_y,
            source=MaskSource.HAND_PAINTED,
        )
        command = AttachMask(
            expected_revision=document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=layer_id,
            mask=mask,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"mask arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={identity: payload},
        report_name="edit.mask",
    )


def edit_mask_paint_command(
    root_argument: str,
    layer_id_argument: str,
    mask_argument: str,
    *,
    before_argument: str,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Commit a checked full-mask paint result and its exact sparse tile delta."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    document = opened.generation.document
    source = _resolved_file(mask_argument, "painted mask")
    try:
        layer_id = _layer_id(layer_id_argument)
        before_id = _object_id(before_argument, "before-mask identity")
        layer = document.layer_map.get(layer_id)
        current = None if layer is None else getattr(layer, "mask", None)
        if current is None:
            raise CommandError("mask-paint target has no current mask", ExitCode.INVALID_DATA)
        if current.object_id != before_id:
            raise CommandError(
                "mask-paint before identity is stale",
                ExitCode.INVALID_DATA,
            )
        expected_bytes = current.width * current.height
        if expected_bytes > limits.max_object_bytes:
            raise CommandError("painted mask exceeds the object byte ceiling", ExitCode.INVALID_DATA)
        payload = _bounded_bytes(source, "painted mask", expected_bytes)
        if len(payload) != expected_bytes:
            raise CommandError(
                "painted mask bytes differ from the current Y u8 geometry",
                ExitCode.INVALID_DATA,
            )
        record = next(
            (
                item
                for item in opened.generation.objects
                if item.object_id == current.object_id
            ),
            None,
        )
        if record is None:
            raise CommandError(
                "current mask is absent from the generation closure",
                ExitCode.INVALID_DATA,
            )
        before_payload = ObjectStore(ProjectLayout(root), limits).read(record)
        extent = Rect(current.origin_x, current.origin_y, current.width, current.height)
        before_tiles = mask_digest_index(before_payload, extent)
        after_tiles = mask_digest_index(payload, extent)
        changed_tiles = tuple(
            after
            for before, after in zip(before_tiles, after_tiles, strict=True)
            if before.digest != after.digest
        )
        if not changed_tiles:
            raise CommandError("painted mask changes zero tiles", ExitCode.INVALID_DATA)
        changed_refs = tuple(
            sorted({item.digest for item in changed_tiles}, key=lambda item: item.value)
        )
        identity = ObjectId.from_bytes(payload)
        command = paint_mask(
            document,
            new_revision=_revision_id(revision_id_argument),
            layer_id=layer_id,
            mask=MaskObject(
                object_id=identity,
                width=current.width,
                height=current.height,
                origin_x=current.origin_x,
                origin_y=current.origin_y,
                source=MaskSource.HAND_PAINTED,
            ),
            changed_tile_refs=changed_refs,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"mask-paint arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={identity: payload},
        resolved_objects=tuple(
            ResolvedObject(item.digest, item.rectangle.width * item.rectangle.height)
            for item in changed_tiles
        ),
        report_name="edit.mask-paint",
        detail_rows=(
            ("changedTiles", counted(len(changed_tiles), len(after_tiles))),
            ("changedTileRefs", counted(len(changed_refs), len(changed_tiles))),
        ),
        detail_data={
            "beforeMaskSha256": before_id.value,
            "maskSha256": identity.value,
            "changedTileCount": len(changed_tiles),
            "tileCount": len(after_tiles),
            "changedTileRefCount": len(changed_refs),
        },
    )


def edit_layer_command(
    root_argument: str,
    layer_id_argument: str,
    *,
    revision_id_argument: str | None,
    name: str | None,
    visible: bool | None,
    opacity_u16: int | None,
    blend_mode_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Change validated common layer properties in one atomic generation."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        blend_mode = (
            None if blend_mode_argument is None else BlendMode(blend_mode_argument)
        )
        command = SetLayerProperty(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            name=name,
            visible=visible,
            opacity_u16=opacity_u16,
            blend_mode=blend_mode,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"layer arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.layer",
    )


def edit_group_command(
    root_argument: str,
    *,
    name: str,
    layer_id_argument: str | None,
    revision_id_argument: str | None,
    parent_id_argument: str | None,
    index: int,
    limits: ProjectLimits,
) -> Outcome:
    """Add one empty editable group without moving existing layers implicitly."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        layer = GroupLayer(
            layer_id=_layer_id(layer_id_argument),
            name=name,
            child_layer_ids=(),
        )
        command = AddLayer(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer=layer,
            parent_id=_optional_layer_id(parent_id_argument, "parent layer identity"),
            index=index,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"group arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.group",
    )


def edit_text_command(
    root_argument: str,
    font_argument: str,
    *,
    text: str,
    width: int,
    height: int,
    alignment_argument: str,
    language: str,
    face_index: int,
    axis_arguments: tuple[str, ...],
    preview_argument: str,
    name: str,
    layer_id_argument: str | None,
    revision_id_argument: str | None,
    parent_id_argument: str | None,
    index: int,
    limits: ProjectLimits,
) -> Outcome:
    """Add editable text with one copied pinned font and declared preview asset."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    font_source = _resolved_file(font_argument, "font carrier")
    font_payload = _bounded_bytes(font_source, "font carrier", limits.max_object_bytes)
    try:
        layer_id = _layer_id(layer_id_argument)
        editable = _editable_text(
            font_payload,
            text=text,
            width=width,
            height=height,
            alignment_argument=alignment_argument,
            language=language,
            face_index=face_index,
            axis_arguments=axis_arguments,
            preview_argument=preview_argument,
        )
        command = add_text_layer(
            opened.generation.document,
            new_revision=_revision_id(revision_id_argument),
            layer_id=layer_id,
            name=name,
            editable=editable,
            parent_id=_optional_layer_id(parent_id_argument, "parent layer identity"),
            index=index,
        )
    except CommandError:
        raise
    except TextValidationError as exc:
        raise CommandError(f"text cannot be added: {exc}", ExitCode.INVALID_DATA) from exc
    except (TypeError, ValueError) as exc:
        raise CommandError(f"text arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={editable.font_digest: font_payload},
        report_name="edit.text",
        detail_rows=(
            ("fontObject", counted(1, 1)),
            ("fontAxes", counted(len(editable.axes), len(editable.axes))),
        ),
        detail_data={
            "fontSha256": editable.font_digest.value,
            "fontAxisCount": len(editable.axes),
            "previewAssetSha256": editable.preview_asset_digest.value,
        },
    )


def edit_text_set_command(
    root_argument: str,
    layer_id_argument: str,
    font_argument: str,
    *,
    text: str,
    width: int,
    height: int,
    alignment_argument: str,
    language: str,
    face_index: int,
    axis_arguments: tuple[str, ...],
    preview_argument: str,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Replace editable text and its complete primary-font/layout identity."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    font_source = _resolved_file(font_argument, "font carrier")
    font_payload = _bounded_bytes(font_source, "font carrier", limits.max_object_bytes)
    try:
        layer_id = _layer_id(layer_id_argument)
        current = opened.generation.document.layer_map.get(layer_id)
        editable = _editable_text(
            font_payload,
            text=text,
            width=width,
            height=height,
            alignment_argument=alignment_argument,
            language=language,
            face_index=face_index,
            axis_arguments=axis_arguments,
            preview_argument=preview_argument,
            fallbacks=() if not isinstance(current, TextLayer) else current.fallbacks,
        )
        command = edit_text_layer(
            opened.generation.document,
            new_revision=_revision_id(revision_id_argument),
            layer_id=layer_id,
            editable=editable,
        )
    except CommandError:
        raise
    except TextValidationError as exc:
        raise CommandError(f"text cannot be changed: {exc}", ExitCode.INVALID_DATA) from exc
    except (TypeError, ValueError) as exc:
        raise CommandError(f"text-set arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={editable.font_digest: font_payload},
        report_name="edit.text-set",
        detail_rows=(
            ("fontObject", counted(1, 1)),
            ("fontAxes", counted(len(editable.axes), len(editable.axes))),
        ),
        detail_data={
            "fontSha256": editable.font_digest.value,
            "fontAxisCount": len(editable.axes),
            "previewAssetSha256": editable.preview_asset_digest.value,
        },
    )


def edit_flatten_result_command(
    root_argument: str,
    output_argument: str,
    *,
    source_layer_arguments: tuple[str, ...],
    media_type_argument: str,
    width: int,
    height: int,
    profile_argument: str,
    name: str,
    layer_id_argument: str | None,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Commit one completed local flatten result without claiming renderer credit."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    output_source = _resolved_file(output_argument, "flatten output")
    payload = _bounded_bytes(output_source, "flatten output", limits.max_object_bytes)
    identity = ObjectId.from_bytes(payload)
    try:
        source_ids = tuple(
            _layer_id(item, "flatten source layer identity")
            for item in source_layer_arguments
        )
        output_asset = AssetRef(
            digest=identity,
            byte_count=len(payload),
            media_type=MediaType(media_type_argument),
            width=width,
            height=height,
            profile_digest=_object_id(profile_argument, "flatten profile identity"),
            import_policy=ImportPolicy.COPIED,
        )
        output_layer = PixelLayer(
            layer_id=_layer_id(layer_id_argument, "flatten output layer identity"),
            name=name,
            asset_digest=identity,
        )
        command = FlattenLayers(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            source_layer_ids=source_ids,
            output_asset=output_asset,
            output_layer=output_layer,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"flatten-result arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={identity: payload},
        report_name="edit.flatten-result",
        detail_rows=(
            ("sourceLayers", counted(len(source_ids), len(source_ids))),
            ("renderCredit", counted(0, 1)),
        ),
        detail_data={
            "sourceLayerIds": [item.value for item in source_ids],
            "outputAssetSha256": identity.value,
            "nativeRendererCredited": False,
        },
    )


def edit_adjustment_set_command(
    root_argument: str,
    layer_id_argument: str,
    adjustment_argument: str,
    *,
    parameter_arguments: tuple[str, ...],
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Replace the parameters of one existing adjustment layer."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        adjustment = make_adjustment(
            AdjustmentId(adjustment_argument),
            _parameter_map(parameter_arguments),
        )
        command = ChangeAdjustment(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            adjustment=adjustment,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"adjustment arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.adjustment-set",
    )


def edit_mask_remove_command(
    root_argument: str,
    layer_id_argument: str,
    *,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Remove one existing mask without changing its source layer pixels."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    document = opened.generation.document
    try:
        layer_id = _layer_id(layer_id_argument)
        layer = document.layer_map.get(layer_id)
        if layer is None or getattr(layer, "mask", None) is None:
            raise CommandError("mask target has no mask", ExitCode.INVALID_DATA)
        command = AttachMask(
            expected_revision=document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=layer_id,
            mask=None,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"mask arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.mask-remove",
    )


def edit_layer_remove_command(
    root_argument: str,
    layer_id_argument: str,
    *,
    recursive: bool,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Remove one layer; non-empty groups require explicit recursive authority."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        command = RemoveLayer(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            recursive=recursive,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"layer arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.layer-remove",
    )


def edit_layer_move_command(
    root_argument: str,
    layer_id_argument: str,
    *,
    parent_id_argument: str | None,
    index: int,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Move one layer to an exact root/group position."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        command = ReorderLayer(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            parent_id=_optional_layer_id(parent_id_argument, "parent layer identity"),
            index=index,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"layer arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.layer-move",
    )


def edit_transform_command(
    root_argument: str,
    layer_id_argument: str,
    coefficients: tuple[float, float, float, float, float, float],
    *,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Replace one layer's checked affine transform."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        command = SetTransform(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            transform=AffineTransform(*coefficients),
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"transform arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.transform",
    )


def edit_crop_command(
    root_argument: str,
    *,
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Replace checked canvas geometry without resampling layer pixels."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    try:
        command = CropCanvas(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            canvas=Canvas(width, height, origin_x, origin_y),
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"crop arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.crop",
    )


def edit_selection_command(
    root_argument: str,
    selection_argument: str,
    *,
    kind_argument: str,
    x: int,
    y: int,
    width: int,
    height: int,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Set one bounded vector or raster selection object."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    source = _resolved_file(selection_argument, "selection")
    payload = _bounded_bytes(source, "selection", limits.max_object_bytes)
    if not payload:
        raise CommandError("selection object is empty", ExitCode.INVALID_DATA)
    identity = ObjectId.from_bytes(payload)
    try:
        kind = SelectionKind(kind_argument)
        bounds = Rect(x, y, width, height)
        if kind is SelectionKind.RASTER and len(payload) != width * height:
            raise CommandError(
                "raster selection bytes differ from its Y u8 bounds",
                ExitCode.INVALID_DATA,
            )
        command = SetSelection(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            selection=Selection(kind, identity, bounds),
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(f"selection arguments are invalid: {exc}", ExitCode.USAGE) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={identity: payload},
        report_name="edit.selection",
    )


def edit_mask_from_selection_command(
    root_argument: str,
    layer_id_argument: str,
    *,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Attach the active raster selection as a lossless editable layer mask."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    selection = opened.generation.document.selection
    if selection is None:
        raise CommandError("document has no selection", ExitCode.INVALID_DATA)
    if selection.kind is not SelectionKind.RASTER:
        raise CommandError(
            "mask conversion requires a raster selection",
            ExitCode.INVALID_DATA,
        )
    expected_bytes = selection.bounds.width * selection.bounds.height
    record = next(
        (
            item
            for item in opened.generation.objects
            if item.object_id == selection.object_id
        ),
        None,
    )
    if record is None or record.byte_count != expected_bytes:
        raise CommandError(
            "raster selection bytes differ from its Y u8 bounds",
            ExitCode.INVALID_DATA,
        )
    try:
        mask = selection_to_mask(selection, selection.object_id)
        command = AttachMask(
            expected_revision=opened.generation.document.revision_id,
            new_revision=_revision_id(revision_id_argument),
            layer_id=_layer_id(layer_id_argument),
            mask=mask,
        )
    except CommandError:
        raise
    except (TypeError, ValueError) as exc:
        raise CommandError(
            f"selection mask arguments are invalid: {exc}",
            ExitCode.USAGE,
        ) from exc
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.mask-from-selection",
        detail_rows=(
            ("selectionObjectsReused", counted(1, 1)),
            ("newObjectPayloads", counted(0, 0)),
        ),
        detail_data={
            "selectionSha256": selection.object_id.value,
            "selectionObjectReuseCount": 1,
            "newObjectPayloadCount": 0,
        },
    )


def edit_selection_clear_command(
    root_argument: str,
    *,
    revision_id_argument: str | None,
    limits: ProjectLimits,
) -> Outcome:
    """Clear one existing selection through a new immutable revision."""

    root = _resolved_directory(root_argument, "project root")
    opened = _opened(root, limits)
    if opened.generation.document.selection is None:
        raise CommandError("document has no selection", ExitCode.INVALID_DATA)
    command = SetSelection(
        expected_revision=opened.generation.document.revision_id,
        new_revision=_revision_id(revision_id_argument),
        selection=None,
    )
    return _commit_edit(
        root_argument,
        limits,
        command,
        payloads={},
        report_name="edit.selection-clear",
    )


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
    "edit_adjustment_command",
    "edit_adjustment_set_command",
    "edit_crop_command",
    "edit_group_command",
    "edit_import_command",
    "edit_layer_command",
    "edit_layer_move_command",
    "edit_layer_remove_command",
    "edit_mask_command",
    "edit_mask_remove_command",
    "edit_selection_clear_command",
    "edit_selection_command",
    "edit_transform_command",
    "export_preset_command",
    "export_verify_command",
    "ops_diagnostics_command",
    "ops_providers_command",
    "project_create_command",
    "project_gc_command",
    "project_generations_command",
    "project_info_command",
    "project_layers_command",
    "project_recover_command",
    "project_verify_command",
    "version_command",
)
