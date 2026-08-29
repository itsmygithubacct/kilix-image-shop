"""Open-time validation and explicit unreachable-generation recovery."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from kilix_image_shop.domain.assets import ImportPolicy
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import DocumentId, ObjectId

from .generations import (
    Generation,
    _commit_head,
    _load_generation_from_directory,
    _validate_document_limits,
    generation_digest,
    parse_head,
    parse_object_records,
    required_project_objects,
)
from .layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    fsync_directory,
    read_regular_file,
    require_directory,
    require_regular_file,
    write_new_file,
)
from .locking import ProjectWriterLock
from .objects import ObjectStore


OPEN_VALIDATION_CLASSES = (
    "project-identity",
    "head-syntax",
    "generation-digest",
    "manifest-schema",
    "aggregate-limits",
    "path-rules",
    "recursion-layer-limits",
    "object-presence",
    "object-hashes",
    "external-reference-drift",
)


class OpenValidationError(StoreError):
    def __init__(self, validation_class: str, message: str) -> None:
        if validation_class not in OPEN_VALIDATION_CLASSES:
            raise ValueError("unknown open-validation class")
        self.validation_class = validation_class
        super().__init__(f"{validation_class}: {message}")


@dataclass(frozen=True, slots=True)
class OpenedProject:
    layout: ProjectLayout
    generation: Generation
    validated_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.validated_classes != OPEN_VALIDATION_CLASSES:
            raise StoreError("opened project lacks the complete validation population")


_Result = TypeVar("_Result")


def _classed(validation_class: str, operation: Callable[[], _Result]) -> _Result:
    try:
        return operation()
    except OpenValidationError:
        raise
    except (OSError, ValueError, StoreError) as exc:
        raise OpenValidationError(validation_class, str(exc)) from exc


def _carrier_bytes(
    layout: ProjectLayout,
    generation_id: ObjectId,
    limits: ProjectLimits,
) -> tuple[Path, bytes, bytes]:
    path = layout.generation_path(generation_id)
    require_directory(path)
    names = {entry.name for entry in path.iterdir()}
    if names != {"manifest.json", "objects.json"}:
        raise StoreError("generation directory has missing or uncontrolled entries")
    manifest = read_regular_file(
        path / "manifest.json", maximum_bytes=limits.max_manifest_bytes
    )
    objects = read_regular_file(
        path / "objects.json", maximum_bytes=limits.max_manifest_bytes
    )
    return path, manifest, objects


def _external_path(layout: ProjectLayout, locator: str, policy: ImportPolicy) -> Path:
    path = Path(locator)
    return path if policy is ImportPolicy.EXTERNAL_ABSOLUTE else layout.root.parent / path


def _validate_candidate(
    layout: ProjectLayout,
    generation_id: ObjectId,
    limits: ProjectLimits,
    metadata_id: DocumentId,
) -> Generation:
    path, manifest_bytes, objects_bytes = _classed(
        "path-rules", lambda: _carrier_bytes(layout, generation_id, limits)
    )

    def validate_generation_digest() -> None:
        if generation_digest(manifest_bytes, objects_bytes) != generation_id:
            raise StoreError("generation identity mismatch")

    _classed("generation-digest", validate_generation_digest)

    def parse_document() -> DocumentState:
        document = DocumentState.from_json_bytes(
            manifest_bytes,
            max_manifest_bytes=limits.max_manifest_bytes,
        )
        if document.canonical_bytes() != manifest_bytes:
            raise StoreError("project manifest is not canonical")
        if document.document_id != metadata_id:
            raise StoreError("manifest and project metadata identities differ")
        return document

    document = _classed("manifest-schema", parse_document)
    records = _classed(
        "aggregate-limits", lambda: parse_object_records(objects_bytes, limits)
    )
    _classed(
        "recursion-layer-limits",
        lambda: _validate_document_limits(document, limits),
    )

    def validate_paths_and_closure() -> None:
        require_directory(path)
        require_regular_file(path / "manifest.json")
        require_regular_file(path / "objects.json")
        if {item.object_id for item in records} != set(required_project_objects(document)):
            raise StoreError("generation object closure differs from document reachability")
        for record in records:
            object_path = layout.object_path(record.object_id)
            parent = object_path.parent
            require_directory(parent)
            try:
                metadata = object_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StoreError("reachable object path cannot be inspected") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise StoreError("reachable object is not a real regular file")

    _classed("path-rules", validate_paths_and_closure)

    def validate_presence() -> None:
        for record in records:
            try:
                metadata = layout.object_path(record.object_id).lstat()
            except OSError as exc:
                raise StoreError("reachable object is absent") from exc
            if metadata.st_size != record.byte_count:
                raise StoreError("reachable object byte count differs from its record")

    _classed("object-presence", validate_presence)
    object_store = ObjectStore(layout, limits)
    _classed(
        "object-hashes",
        lambda: tuple(object_store.verify(record) for record in records),
    )

    def validate_external_references() -> None:
        for asset in document.assets:
            if asset.import_policy is ImportPolicy.COPIED:
                continue
            assert asset.locator is not None
            external = _external_path(layout, asset.locator, asset.import_policy)
            payload = read_regular_file(
                external,
                maximum_bytes=min(limits.max_object_bytes, asset.byte_count),
            )
            if len(payload) != asset.byte_count:
                raise StoreError("external reference byte count drifted")
            if hashlib.sha256(payload).hexdigest() != asset.digest.value:
                raise StoreError("external reference digest drifted")

    _classed("external-reference-drift", validate_external_references)
    return Generation(generation_id, document, records, manifest_bytes, objects_bytes)


def open_project(layout: ProjectLayout, limits: ProjectLimits) -> OpenedProject:
    """Run all 10/10 validation classes before exposing a writable document."""

    def identity() -> DocumentId:
        layout.verify_structure()
        return layout.read_metadata()

    metadata_id = _classed("project-identity", identity)
    generation_id = _classed(
        "head-syntax",
        lambda: parse_head(read_regular_file(layout.head, maximum_bytes=65)),
    )
    generation = _validate_candidate(layout, generation_id, limits, metadata_id)
    return OpenedProject(layout, generation, OPEN_VALIDATION_CLASSES)


@dataclass(frozen=True, slots=True)
class RecoveryPreview:
    layout: ProjectLayout
    candidate: Generation
    original_head_bytes: bytes
    original_head_sha256: ObjectId

    def __post_init__(self) -> None:
        if ObjectId.from_bytes(self.original_head_bytes) != self.original_head_sha256:
            raise StoreError("recovery preview does not bind the original HEAD bytes")


def preview_recovery(
    layout: ProjectLayout,
    candidate_id: ObjectId,
    limits: ProjectLimits,
) -> RecoveryPreview:
    """Validate one named orphan candidate without changing or replacing HEAD."""

    metadata_id = _classed(
        "project-identity",
        lambda: (layout.verify_structure(), layout.read_metadata())[1],
    )
    original = read_regular_file(layout.head, maximum_bytes=4096)
    try:
        current = parse_head(original)
    except StoreError:
        current = None
    if current == candidate_id:
        raise StoreError("recovery candidate is already the current HEAD")
    candidate = _validate_candidate(layout, candidate_id, limits, metadata_id)
    return RecoveryPreview(layout, candidate, original, ObjectId.from_bytes(original))


def apply_recovery(preview: RecoveryPreview, limits: ProjectLimits) -> OpenedProject:
    """Revalidate and explicitly select a previewed candidate, retaining old bytes."""

    if not isinstance(preview, RecoveryPreview):
        raise StoreError("recovery apply requires a typed preview")
    layout = preview.layout
    with ProjectWriterLock(layout):
        current_bytes = read_regular_file(layout.head, maximum_bytes=4096)
        if current_bytes != preview.original_head_bytes:
            raise StoreError("HEAD changed after recovery preview")
        metadata_id = layout.read_metadata()
        candidate = _validate_candidate(
            layout,
            preview.candidate.generation_id,
            limits,
            metadata_id,
        )
        if candidate != preview.candidate:
            raise StoreError("recovery candidate changed after preview")
        slot = layout.autosave / f"recovery-{preview.original_head_sha256.value}"
        try:
            slot.mkdir(mode=0o700)
            write_new_file(slot / "HEAD", preview.original_head_bytes)
        except FileExistsError:
            retained = read_regular_file(slot / "HEAD", maximum_bytes=4096)
            if retained != preview.original_head_bytes:
                raise StoreError("recovery retention slot has conflicting bytes")
        except OSError as exc:
            raise StoreError("original HEAD bytes cannot be retained") from exc
        fsync_directory(slot)
        fsync_directory(layout.autosave)
        _commit_head(layout, candidate.generation_id)
    return open_project(layout, limits)


def list_recovery_candidates(layout: ProjectLayout) -> tuple[ObjectId, ...]:
    """List immutable generation identities without selecting any of them."""

    require_directory(layout.generations)
    values: list[ObjectId] = []
    for entry in layout.generations.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            identity = ObjectId(entry.name)
        except ValueError as exc:
            raise StoreError("generations directory contains an uncontrolled entry") from exc
        require_directory(entry)
        values.append(identity)
    return tuple(sorted(values, key=lambda item: item.value))
