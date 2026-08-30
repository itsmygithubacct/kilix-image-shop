"""Full-resolution bounded-tile export with verified atomic publication."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId
from kilix_image_shop.engine.api import (
    CancelToken,
    ImageEngine,
    PixelSpec,
    TileResult,
)
from kilix_image_shop.render.plan import RenderPlan
from kilix_image_shop.render.scheduler import partition_tiles

from .presets import (
    ExportFormat,
    ExportPreset,
    MetadataPolicy,
    object_closure_digest,
)
from .provenance import (
    ExportArtifact,
    ExportProvenance,
    ExportProvenanceError,
    project_export_provenance,
)


SIDECAR_SUFFIX = ".provenance.json"


class ExportPipelineError(RuntimeError):
    """Export failed before an admissible destination publication."""


class ExportPublicationIndeterminate(ExportPipelineError):
    """Image and sidecar replaced consistently, but directory fsync failed."""


@dataclass(frozen=True, slots=True)
class ExportLimits:
    max_raw_bytes: int
    max_encoded_bytes: int
    max_sidecar_bytes: int
    max_tiles: int

    def __post_init__(self) -> None:
        for field in (
            "max_raw_bytes",
            "max_encoded_bytes",
            "max_sidecar_bytes",
            "max_tiles",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExportPipelineError(f"{field} must be finite and positive")


@dataclass(frozen=True, slots=True)
class CodecInspection:
    export_format: ExportFormat
    width: int
    height: int
    profile_digest: ObjectId
    metadata_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.export_format, ExportFormat):
            raise ExportPipelineError("codec inspection format is outside the closed set")
        for value, label in ((self.width, "width"), (self.height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExportPipelineError(f"codec inspection {label} must be positive")
        if not isinstance(self.profile_digest, ObjectId):
            raise ExportPipelineError("codec inspection profile must be content-addressed")
        if not isinstance(self.metadata_keys, tuple) or any(
            not isinstance(item, str) for item in self.metadata_keys
        ):
            raise ExportPipelineError("codec inspection metadata must be immutable strings")
        if self.metadata_keys != tuple(sorted(set(self.metadata_keys))):
            raise ExportPipelineError("codec inspection metadata must be sorted and unique")


@runtime_checkable
class CodecWorker(Protocol):
    """An owned isolated codec process addressed only through inherited FDs."""

    @property
    def isolated(self) -> bool: ...

    def encode(
        self,
        raw_fd: int,
        output_fd: int,
        *,
        raw_spec: PixelSpec,
        preset: ExportPreset,
        cancel: CancelToken,
    ) -> None: ...

    def inspect(
        self,
        output_fd: int,
        *,
        preset: ExportPreset,
        cancel: CancelToken,
    ) -> CodecInspection: ...


@dataclass(frozen=True, slots=True)
class ExportResult:
    destination: Path
    sidecar: Path
    artifact: ExportArtifact
    provenance: ExportProvenance
    tile_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.destination, Path) or not self.destination.is_absolute():
            raise ExportPipelineError("export result destination must be absolute")
        if self.sidecar != self.destination.with_name(
            self.destination.name + SIDECAR_SUFFIX
        ):
            raise ExportPipelineError("export result sidecar path is not canonical")
        if not isinstance(self.artifact, ExportArtifact) or not isinstance(
            self.provenance, ExportProvenance
        ):
            raise ExportPipelineError("export result lacks artifact or provenance")
        if self.artifact != self.provenance.artifact:
            raise ExportPipelineError("export result artifact differs from its sidecar")
        if (
            isinstance(self.tile_count, bool)
            or not isinstance(self.tile_count, int)
            or self.tile_count <= 0
        ):
            raise ExportPipelineError("export result tile count must be positive")


@dataclass(frozen=True, slots=True)
class _TargetSnapshot:
    exists: bool
    device: int | None = None
    inode: int | None = None
    size: int | None = None
    modified_ns: int | None = None
    changed_ns: int | None = None


def _target_snapshot(parent_fd: int, name: str) -> _TargetSnapshot:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _TargetSnapshot(False)
    except OSError as exc:
        raise ExportPipelineError("export target cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ExportPipelineError("export target must be absent or a regular file")
    return _TargetSnapshot(
        True,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_unchanged(parent_fd: int, name: str, expected: _TargetSnapshot) -> None:
    if _target_snapshot(parent_fd, name) != expected:
        raise ExportPipelineError("export target changed during staging")


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise ExportPipelineError("export staging write made no progress")
        offset += written


def _pwrite_all(fd: int, payload: bytes, offset: int) -> None:
    written = 0
    while written < len(payload):
        amount = os.pwrite(fd, payload[written:], offset + written)
        if amount <= 0:
            raise ExportPipelineError("export tile write made no progress")
        written += amount


def _read_bounded(fd: int, maximum_bytes: int, *, label: str) -> bytes:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise ExportPipelineError(f"{label} is not a non-empty regular file")
    if metadata.st_size > maximum_bytes:
        raise ExportPipelineError(f"{label} exceeds its finite byte budget")
    payload = bytearray()
    offset = 0
    while offset < metadata.st_size:
        chunk = os.pread(fd, min(1_048_576, metadata.st_size - offset), offset)
        if not chunk:
            raise ExportPipelineError(f"{label} ended before its declared size")
        payload.extend(chunk)
        offset += len(chunk)
    return bytes(payload)


def _digest_bounded(fd: int, maximum_bytes: int) -> tuple[int, ObjectId]:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise ExportPipelineError("encoded export is not a non-empty regular file")
    if metadata.st_size > maximum_bytes:
        raise ExportPipelineError("encoded export exceeds its finite byte budget")
    digest = hashlib.sha256()
    offset = 0
    while offset < metadata.st_size:
        chunk = os.pread(fd, min(1_048_576, metadata.st_size - offset), offset)
        if not chunk:
            raise ExportPipelineError("encoded export ended before its declared size")
        digest.update(chunk)
        offset += len(chunk)
    return metadata.st_size, ObjectId(digest.hexdigest())


def _validate_tile(
    result: TileResult,
    request: object,
    expected_spec: PixelSpec,
) -> bytes:
    from kilix_image_shop.engine.api import TileRequest

    if not isinstance(request, TileRequest) or not isinstance(result, TileResult):
        raise ExportPipelineError("engine returned an untyped export tile")
    if (
        result.source != request.source
        or result.destination != request.destination
        or result.level != 0
        or result.spec != expected_spec
        or result.revision != request.revision
        or result.owned_bytes is None
    ):
        raise ExportPipelineError("engine export tile differs from its request")
    expected = (
        result.destination.width
        * result.destination.height
        * expected_spec.pixel_format.bytes_per_pixel
    )
    if (
        len(result.owned_bytes) != expected
        or ObjectId.from_bytes(result.owned_bytes) != result.payload_digest
    ):
        raise ExportPipelineError("engine export tile payload failed size or digest checks")
    return result.owned_bytes


def _write_tile_rows(
    raw_fd: int,
    result: TileResult,
    payload: bytes,
    destination: Rect,
    spec: PixelSpec,
    *,
    cancel: CancelToken,
) -> None:
    tile = result.destination
    if not tile.is_within(destination):
        raise ExportPipelineError("export tile leaves its output geometry")
    bytes_per_pixel = spec.pixel_format.bytes_per_pixel
    row_bytes = tile.width * bytes_per_pixel
    for row in range(tile.height):
        # The export-row cancellation checkpoint is intentionally inside the loop.
        cancel.raise_if_cancelled()
        source_offset = row * row_bytes
        target_row = tile.y - destination.y + row
        target_offset = (
            target_row * destination.width + tile.x - destination.x
        ) * bytes_per_pixel
        _pwrite_all(
            raw_fd,
            payload[source_offset : source_offset + row_bytes],
            target_offset,
        )


class _Stages:
    def __init__(self, destination: Path) -> None:
        if not isinstance(destination, Path) or not destination.is_absolute():
            raise ExportPipelineError("export destination must be an absolute path")
        if not destination.name or destination.name in {".", ".."}:
            raise ExportPipelineError("export destination basename is invalid")
        parent = destination.parent
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise ExportPipelineError("export parent cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ExportPipelineError("export parent must be a real directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.parent_fd = -1
        self.raw_fd = -1
        self.image_fd = -1
        self.sidecar_fd = -1
        self._published_image = False
        self._published_sidecar = False
        self._has_backup = False
        try:
            self.parent_fd = os.open(parent, flags)
        except OSError as exc:
            raise ExportPipelineError("export parent cannot be opened safely") from exc
        try:
            self.destination = destination
            self.destination_name = destination.name
            self.sidecar_name = destination.name + SIDECAR_SUFFIX
            self.destination_snapshot = _target_snapshot(
                self.parent_fd,
                self.destination_name,
            )
            self.sidecar_snapshot = _target_snapshot(self.parent_fd, self.sidecar_name)
            token = uuid.uuid4().hex
            self.raw_name = f".kilix-export-{token}.raw"
            self.image_name = f".kilix-export-{token}.image"
            self.sidecar_stage_name = f".kilix-export-{token}.sidecar"
            self.sidecar_backup_name = f".kilix-export-{token}.backup"
            create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            self.raw_fd = os.open(
                self.raw_name,
                create_flags,
                0o600,
                dir_fd=self.parent_fd,
            )
            self.image_fd = os.open(
                self.image_name,
                create_flags,
                0o600,
                dir_fd=self.parent_fd,
            )
            self.sidecar_fd = os.open(
                self.sidecar_stage_name,
                create_flags,
                0o600,
                dir_fd=self.parent_fd,
            )
        except Exception as exc:
            self.close()
            if isinstance(exc, ExportPipelineError):
                raise
            raise ExportPipelineError("private export staging files cannot be created") from exc

    @property
    def sidecar_path(self) -> Path:
        return self.destination.with_name(self.sidecar_name)

    def seal_raw(self) -> None:
        try:
            os.fsync(self.raw_fd)
            os.close(self.raw_fd)
            self.raw_fd = -1
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            self.raw_fd = os.open(
                self.raw_name,
                flags,
                dir_fd=self.parent_fd,
            )
        except OSError as exc:
            raise ExportPipelineError("raw export staging cannot be sealed read-only") from exc

    def _rollback_sidecar(self) -> None:
        try:
            if self._has_backup:
                os.replace(
                    self.sidecar_backup_name,
                    self.sidecar_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
                self._has_backup = False
            else:
                os.unlink(self.sidecar_name, dir_fd=self.parent_fd)
            self._published_sidecar = False
            os.fsync(self.parent_fd)
        except OSError as exc:
            raise ExportPublicationIndeterminate(
                "export image stayed unchanged but sidecar rollback was indeterminate"
            ) from exc

    def publish(self) -> None:
        _require_unchanged(
            self.parent_fd,
            self.destination_name,
            self.destination_snapshot,
        )
        _require_unchanged(
            self.parent_fd,
            self.sidecar_name,
            self.sidecar_snapshot,
        )
        try:
            if self.sidecar_snapshot.exists:
                os.link(
                    self.sidecar_name,
                    self.sidecar_backup_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
                self._has_backup = True
            os.replace(
                self.sidecar_stage_name,
                self.sidecar_name,
                src_dir_fd=self.parent_fd,
                dst_dir_fd=self.parent_fd,
            )
            self._published_sidecar = True
            try:
                os.fsync(self.parent_fd)
                os.replace(
                    self.image_name,
                    self.destination_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
                self._published_image = True
            except OSError:
                self._rollback_sidecar()
                raise
            try:
                os.fsync(self.parent_fd)
            except OSError as exc:
                raise ExportPublicationIndeterminate(
                    "export image and sidecar were replaced but directory fsync failed"
                ) from exc
            if self._has_backup:
                try:
                    os.unlink(self.sidecar_backup_name, dir_fd=self.parent_fd)
                except OSError as exc:
                    raise ExportPublicationIndeterminate(
                        "export replacement committed but backup retirement failed"
                    ) from exc
                self._has_backup = False
                try:
                    os.fsync(self.parent_fd)
                except OSError as exc:
                    raise ExportPublicationIndeterminate(
                        "export replacement committed but backup retirement was not durable"
                    ) from exc
        except ExportPublicationIndeterminate:
            raise
        except OSError as exc:
            raise ExportPipelineError("verified export could not be atomically published") from exc

    def close(self) -> None:
        for field in ("raw_fd", "image_fd", "sidecar_fd"):
            descriptor = getattr(self, field, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, field, -1)
        parent_fd = getattr(self, "parent_fd", -1)
        if parent_fd >= 0:
            for field, published in (
                ("raw_name", False),
                ("image_name", getattr(self, "_published_image", False)),
                (
                    "sidecar_stage_name",
                    getattr(self, "_published_sidecar", False),
                ),
                ("sidecar_backup_name", False),
            ):
                name = getattr(self, field, None)
                if name is None or published:
                    continue
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            try:
                os.close(parent_fd)
            except OSError:
                pass
            self.parent_fd = -1


def export_document(
    document: DocumentState,
    plan: RenderPlan,
    preset: ExportPreset,
    destination: Path,
    *,
    engine: ImageEngine,
    worker: CodecWorker,
    limits: ExportLimits,
    cancel: CancelToken,
    include_prompts: bool = False,
) -> ExportResult:
    """Render one immutable generation and publish image plus verified sidecar."""

    cancel.raise_if_cancelled()
    if not isinstance(document, DocumentState) or not isinstance(plan, RenderPlan):
        raise ExportPipelineError("export requires document and render plan")
    if not isinstance(preset, ExportPreset) or not isinstance(limits, ExportLimits):
        raise ExportPipelineError("export requires preset and finite limits")
    if not isinstance(cancel, CancelToken) or not isinstance(worker, CodecWorker):
        raise ExportPipelineError("export requires cancellation and codec ports")
    if worker.isolated is not True:
        raise ExportPipelineError("codec finalization must use an owned isolated worker")
    if (
        document.document_id != preset.document_id
        or document.revision_id != preset.revision
        or document.manifest_digest != preset.document_manifest_digest
        or object_closure_digest(document) != preset.object_closure_digest
        or document.engine_compatibility.digest != preset.compatibility_digest
        or document.engine_compatibility.plugin_tree_digest
        != preset.plugin_tree_digest
        or plan.revision != preset.revision
        or plan.compatibility_digest != preset.compatibility_digest
        or plan.output_bounds != preset.crop
        or plan.output_spec.pixel_format is not preset.working_format
        or plan.output_spec.profile_digest != preset.working_profile
    ):
        raise ExportPipelineError("document, render plan and export preset do not join")
    raw_size = (
        preset.width
        * preset.height
        * plan.output_spec.pixel_format.bytes_per_pixel
    )
    if raw_size > limits.max_raw_bytes:
        raise ExportPipelineError("raw export exceeds its independent staging budget")
    destination_extent = Rect(0, 0, preset.width, preset.height)
    requests = partition_tiles(
        graph_digest=plan.digest,
        source=preset.crop,
        destination=destination_extent,
        level=0,
        spec=plan.output_spec,
        revision=plan.revision,
    )
    if len(requests) > limits.max_tiles:
        raise ExportPipelineError("export tile population exceeds its finite budget")
    stages = _Stages(destination)
    try:
        os.ftruncate(stages.raw_fd, raw_size)
        compiled = engine.compile_graph(plan.graph, cancel=cancel)
        if compiled != plan.digest:
            raise ExportPipelineError("engine compiled a different export plan")
        for request in requests:
            cancel.raise_if_cancelled()
            results = engine.export_tiles((request,), cancel=cancel)
            cancel.raise_if_cancelled()
            if not isinstance(results, tuple) or len(results) != 1:
                raise ExportPipelineError("engine did not return exactly one bounded tile")
            payload = _validate_tile(results[0], request, plan.output_spec)
            _write_tile_rows(
                stages.raw_fd,
                results[0],
                payload,
                destination_extent,
                plan.output_spec,
                cancel=cancel,
            )
        cancel.raise_if_cancelled()
        stages.seal_raw()
        os.ftruncate(stages.image_fd, 0)
        worker.encode(
            stages.raw_fd,
            stages.image_fd,
            raw_spec=plan.output_spec,
            preset=preset,
            cancel=cancel,
        )
        cancel.raise_if_cancelled()
        os.fsync(stages.image_fd)
        inspection = worker.inspect(
            stages.image_fd,
            preset=preset,
            cancel=cancel,
        )
        cancel.raise_if_cancelled()
        if not isinstance(inspection, CodecInspection):
            raise ExportPipelineError("codec worker returned an untyped inspection")
        if (
            inspection.export_format is not preset.export_format
            or inspection.width != preset.width
            or inspection.height != preset.height
            or inspection.profile_digest != preset.output_profile
        ):
            raise ExportPipelineError("encoded export failed format/profile/geometry checks")
        if (
            preset.metadata_policy is MetadataPolicy.STRIP
            and inspection.metadata_keys
        ):
            raise ExportPipelineError("encoded export retained forbidden metadata")
        encoded_size, encoded_digest = _digest_bounded(
            stages.image_fd,
            limits.max_encoded_bytes,
        )
        artifact = ExportArtifact(
            image_digest=encoded_digest,
            byte_count=encoded_size,
            export_format=inspection.export_format,
            width=inspection.width,
            height=inspection.height,
            profile_digest=inspection.profile_digest,
            metadata_keys=inspection.metadata_keys,
        )
        provenance = project_export_provenance(
            document,
            plan,
            preset,
            artifact,
            include_prompts=include_prompts,
        )
        sidecar_payload = provenance.canonical_bytes()
        if len(sidecar_payload) > limits.max_sidecar_bytes:
            raise ExportPipelineError("export sidecar exceeds its finite byte budget")
        os.ftruncate(stages.sidecar_fd, 0)
        os.lseek(stages.sidecar_fd, 0, os.SEEK_SET)
        _write_all(stages.sidecar_fd, sidecar_payload)
        os.fsync(stages.sidecar_fd)
        retained_sidecar = _read_bounded(
            stages.sidecar_fd,
            limits.max_sidecar_bytes,
            label="export sidecar",
        )
        verified = ExportProvenance.from_bytes(
            retained_sidecar,
            maximum_bytes=limits.max_sidecar_bytes,
        )
        verified.validate_join(preset)
        if verified != provenance or verified.artifact.image_digest != artifact.image_digest:
            raise ExportProvenanceError("retained sidecar differs from its export projection")
        # This is the final cancellation point. Publication is one non-cancellable commit.
        cancel.raise_if_cancelled()
        stages.publish()
        return ExportResult(
            destination,
            stages.sidecar_path,
            artifact,
            provenance,
            len(requests),
        )
    except OSError as exc:
        raise ExportPipelineError("export staging failed") from exc
    finally:
        stages.close()


__all__ = (
    "CodecInspection",
    "CodecWorker",
    "ExportLimits",
    "ExportPipelineError",
    "ExportPublicationIndeterminate",
    "ExportResult",
    "SIDECAR_SUFFIX",
    "export_document",
)
