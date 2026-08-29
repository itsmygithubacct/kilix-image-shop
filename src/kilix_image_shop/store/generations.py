"""Immutable generation construction, verification, and atomic HEAD commit."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from kilix_image_shop.domain.assets import ImportPolicy
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, ObjectId
from kilix_image_shop.domain.layers import GroupLayer, TextLayer

from .layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    canonical_json_bytes,
    fsync_directory,
    parse_canonical_json,
    read_regular_file,
    require_directory,
    write_new_file,
)
from .locking import ProjectWriterLock
from .objects import ObjectRecord, ObjectStore, StagedObject


GENERATION_OBJECTS_SCHEMA = "kilix.imageshop.generation-objects/v1"
GENERATION_DIGEST_DOMAIN = b"kilix.imageshop.generation/v1\0"
SAVE_POINT_COUNT = 12
FaultHook = Callable[[int], None]


class SaveConflict(StoreError):
    """The opened generation is no longer the project's current HEAD."""


@dataclass(frozen=True, slots=True)
class Generation:
    generation_id: ObjectId
    document: DocumentState
    objects: tuple[ObjectRecord, ...]
    manifest_bytes: bytes
    objects_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.generation_id, ObjectId) or not isinstance(
            self.document, DocumentState
        ):
            raise StoreError("generation requires typed identity and document")
        if not isinstance(self.objects, tuple) or any(
            not isinstance(item, ObjectRecord) for item in self.objects
        ):
            raise StoreError("generation object closure must be an immutable tuple")
        identities = tuple(item.object_id.value for item in self.objects)
        if identities != tuple(sorted(set(identities))):
            raise StoreError("generation object closure must be sorted and unique")
        if self.document.canonical_bytes() != self.manifest_bytes:
            raise StoreError("generation document differs from its manifest carrier")
        if generation_digest(self.manifest_bytes, self.objects_bytes) != self.generation_id:
            raise StoreError("generation identity differs from its carriers")


def generation_digest(manifest_bytes: bytes, objects_bytes: bytes) -> ObjectId:
    if not isinstance(manifest_bytes, bytes) or not isinstance(objects_bytes, bytes):
        raise StoreError("generation digest requires immutable carrier bytes")
    digest = hashlib.sha256()
    digest.update(GENERATION_DIGEST_DOMAIN)
    digest.update(len(manifest_bytes).to_bytes(8, "big"))
    digest.update(manifest_bytes)
    digest.update(len(objects_bytes).to_bytes(8, "big"))
    digest.update(objects_bytes)
    return ObjectId(digest.hexdigest())


def head_bytes(generation_id: ObjectId) -> bytes:
    if not isinstance(generation_id, ObjectId):
        raise StoreError("HEAD requires a typed generation identity")
    return (generation_id.value + "\n").encode("ascii")


def parse_head(payload: bytes) -> ObjectId:
    if not isinstance(payload, bytes) or len(payload) != 65 or payload[-1:] != b"\n":
        raise StoreError("HEAD must contain one lowercase SHA-256 plus one LF")
    try:
        return ObjectId(payload[:-1].decode("ascii", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreError("HEAD contains a malformed generation identity") from exc


def read_head(layout: ProjectLayout, *, allow_missing: bool = False) -> ObjectId | None:
    try:
        payload = read_regular_file(layout.head, maximum_bytes=65)
    except StoreError:
        if allow_missing:
            try:
                layout.head.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                pass
        raise
    return parse_head(payload)


def required_project_objects(document: DocumentState) -> tuple[ObjectId, ...]:
    """Return exact project-owned object references, excluding explicit externals."""

    values: set[ObjectId] = {
        asset.digest
        for asset in document.assets
        if asset.import_policy is ImportPolicy.COPIED
    }
    if document.selection is not None:
        values.add(document.selection.object_id)
    for layer in document.layers:
        mask = getattr(layer, "mask", None)
        if mask is not None:
            values.add(mask.object_id)
            if mask.source_ref is not None:
                values.add(mask.source_ref)
        if isinstance(layer, TextLayer):
            values.add(layer.font_digest)
            values.update(
                fallback.resolved_font_digest
                for fallback in layer.fallbacks
                if fallback.resolved_font_digest is not None
            )
    return tuple(sorted(values, key=lambda item: item.value))


def _validate_document_limits(document: DocumentState, limits: ProjectLimits) -> None:
    manifest = document.canonical_bytes()
    if len(manifest) > limits.max_manifest_bytes:
        raise StoreError("project manifest exceeds its byte budget")
    if len(document.layers) > limits.max_layers:
        raise StoreError("project layer table exceeds its count budget")
    layer_map = document.layer_map

    def depth(layer_id: LayerId, current: int) -> None:
        if current > limits.max_group_depth:
            raise StoreError("project layer graph exceeds its recursion budget")
        layer = layer_map[layer_id]
        if isinstance(layer, GroupLayer):
            for child in layer.child_layer_ids:
                depth(child, current + 1)

    for root in document.root_layer_ids:
        depth(root, 1)


def _records_bytes(records: tuple[ObjectRecord, ...]) -> bytes:
    return canonical_json_bytes(
        {
            "objects": [record.to_data() for record in records],
            "schema": GENERATION_OBJECTS_SCHEMA,
        }
    )


def parse_object_records(payload: bytes, limits: ProjectLimits) -> tuple[ObjectRecord, ...]:
    value = parse_canonical_json(payload, maximum_bytes=limits.max_manifest_bytes)
    if not isinstance(value, dict) or set(value) != {"objects", "schema"}:
        raise StoreError("generation object carrier has missing or unknown fields")
    if value["schema"] != GENERATION_OBJECTS_SCHEMA:
        raise StoreError("generation object carrier schema is unsupported")
    raw_objects = value["objects"]
    if not isinstance(raw_objects, list) or len(raw_objects) > limits.max_objects:
        raise StoreError("generation object closure is malformed or over budget")
    records: list[ObjectRecord] = []
    for item in raw_objects:
        if not isinstance(item, dict) or set(item) != {"byteCount", "sha256"}:
            raise StoreError("generation object record has missing or unknown fields")
        try:
            record = ObjectRecord(ObjectId.parse(item["sha256"]), item["byteCount"])
        except ValueError as exc:
            raise StoreError("generation object record is malformed") from exc
        if record.byte_count > limits.max_object_bytes:
            raise StoreError("generation object exceeds its per-object byte budget")
        records.append(record)
    result = tuple(records)
    identities = tuple(item.object_id.value for item in result)
    if identities != tuple(sorted(set(identities))):
        raise StoreError("generation object closure is not sorted and unique")
    if sum(item.byte_count for item in result) > limits.max_total_object_bytes:
        raise StoreError("generation object closure exceeds its aggregate byte budget")
    return result


def _resolve_records(
    document: DocumentState,
    payloads: Mapping[ObjectId, bytes],
    store: ObjectStore,
) -> tuple[ObjectRecord, ...]:
    copied_sizes = {
        asset.digest: asset.byte_count
        for asset in document.assets
        if asset.import_policy is ImportPolicy.COPIED
    }
    records: list[ObjectRecord] = []
    for object_id in required_project_objects(document):
        payload = payloads.get(object_id)
        if payload is not None:
            if not isinstance(payload, bytes):
                raise StoreError("object payload table contains a mutable value")
            byte_count = len(payload)
        else:
            path = store.layout.object_path(object_id)
            try:
                byte_count = path.lstat().st_size
            except OSError as exc:
                raise StoreError("reachable project object is absent and has no payload") from exc
        if object_id in copied_sizes and copied_sizes[object_id] != byte_count:
            raise StoreError("copied asset byte count differs from its reachable object")
        records.append(ObjectRecord(object_id, byte_count))
    unknown = set(payloads) - set(required_project_objects(document))
    if unknown:
        raise StoreError("object payload table contains unreachable objects")
    return tuple(records)


def _load_generation_from_directory(
    path: Path,
    expected_id: ObjectId,
    limits: ProjectLimits,
    *,
    verify_objects: ObjectStore | None,
) -> Generation:
    require_directory(path)
    try:
        names = {entry.name for entry in path.iterdir()}
    except OSError as exc:
        raise StoreError("generation directory cannot be enumerated") from exc
    if names != {"manifest.json", "objects.json"}:
        raise StoreError("generation directory has missing or uncontrolled entries")
    manifest_bytes = read_regular_file(
        path / "manifest.json", maximum_bytes=limits.max_manifest_bytes
    )
    objects_bytes = read_regular_file(
        path / "objects.json", maximum_bytes=limits.max_manifest_bytes
    )
    if generation_digest(manifest_bytes, objects_bytes) != expected_id:
        raise StoreError("generation digest differs from its directory identity")
    try:
        document = DocumentState.from_json_bytes(
            manifest_bytes,
            max_manifest_bytes=limits.max_manifest_bytes,
        )
    except ValueError as exc:
        raise StoreError("generation manifest schema validation failed") from exc
    if document.canonical_bytes() != manifest_bytes:
        raise StoreError("generation manifest is not in canonical form")
    _validate_document_limits(document, limits)
    records = parse_object_records(objects_bytes, limits)
    if {record.object_id for record in records} != set(required_project_objects(document)):
        raise StoreError("generation object carrier differs from document reachability")
    if verify_objects is not None:
        for record in records:
            verify_objects.verify(record)
    return Generation(expected_id, document, records, manifest_bytes, objects_bytes)


def load_generation(
    layout: ProjectLayout,
    generation_id: ObjectId,
    limits: ProjectLimits,
    *,
    verify_objects: bool = True,
) -> Generation:
    store = ObjectStore(layout, limits) if verify_objects else None
    return _load_generation_from_directory(
        layout.generation_path(generation_id),
        generation_id,
        limits,
        verify_objects=store,
    )


def _publish_generation(staging: Path, destination: Path, expected: Generation) -> None:
    try:
        os.rename(staging, destination)
    except FileExistsError:
        existing = _load_generation_from_directory(
            destination,
            expected.generation_id,
            ProjectLimits(
                max_manifest_bytes=max(
                    len(expected.manifest_bytes), len(expected.objects_bytes)
                ),
                max_objects=max(1, len(expected.objects)),
                max_object_bytes=max(
                    (item.byte_count for item in expected.objects), default=1
                ),
                max_total_object_bytes=max(sum(item.byte_count for item in expected.objects), 1),
                max_layers=max(len(expected.document.layers), 1),
                max_group_depth=max(len(expected.document.layers), 1),
            ),
            verify_objects=None,
        )
        if (
            existing.manifest_bytes != expected.manifest_bytes
            or existing.objects_bytes != expected.objects_bytes
        ):
            raise StoreError("existing generation identity has different carriers")
        shutil.rmtree(staging)
    except OSError as exc:
        raise StoreError("generation cannot be atomically published") from exc


def _commit_head(layout: ProjectLayout, generation_id: ObjectId) -> None:
    temporary = layout.root / f".HEAD-{uuid.uuid4().hex}"
    try:
        write_new_file(temporary, head_bytes(generation_id))
        os.replace(temporary, layout.head)
        fsync_directory(layout.root)
    except OSError as exc:
        raise StoreError("HEAD cannot be atomically committed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class GenerationStore:
    """Execute the frozen 12/12 save transaction for one project."""

    def __init__(self, layout: ProjectLayout, limits: ProjectLimits) -> None:
        self.layout = layout
        self.limits = limits
        self.objects = ObjectStore(layout, limits)

    def save(
        self,
        document: DocumentState,
        *,
        object_payloads: Mapping[ObjectId, bytes],
        expected_head: ObjectId | None,
        fault_hook: FaultHook | None = None,
    ) -> Generation:
        if not isinstance(document, DocumentState):
            raise StoreError("save requires one immutable document revision")
        if not isinstance(object_payloads, Mapping):
            raise StoreError("save object payloads must be a mapping")
        if expected_head is not None and not isinstance(expected_head, ObjectId):
            raise StoreError("expected HEAD must be a typed generation identity")
        payloads = dict(object_payloads)
        if any(not isinstance(key, ObjectId) for key in payloads):
            raise StoreError("save object payload table contains an untyped identity")
        if any(not isinstance(payload, bytes) for payload in payloads.values()):
            raise StoreError("save object payload table contains a mutable value")
        hook = fault_hook if fault_hook is not None else lambda point: None
        lock = ProjectWriterLock(self.layout)
        staging_objects: tuple[StagedObject, ...] = ()
        staging_generation: Path | None = None
        committed: Generation | None = None
        lock.acquire()
        try:
            try:
                self.layout.head.lstat()
            except FileNotFoundError:
                self.layout.verify_structure(allow_missing_head=True)
            except OSError as exc:
                raise StoreError("project HEAD cannot be inspected") from exc
            else:
                self.layout.verify_structure()
            current = read_head(self.layout, allow_missing=True)
            if current != expected_head:
                raise SaveConflict("project HEAD changed after the document was opened")
            hook(1)

            manifest_bytes = document.canonical_bytes()
            try:
                captured = DocumentState.from_json_bytes(
                    manifest_bytes,
                    max_manifest_bytes=self.limits.max_manifest_bytes,
                )
            except ValueError as exc:
                raise StoreError("captured document revision is invalid") from exc
            if captured != document:
                raise StoreError("captured document revision is not stable")
            hook(2)

            _validate_document_limits(captured, self.limits)
            records = _resolve_records(captured, payloads, self.objects)
            if len(records) > self.limits.max_objects:
                raise StoreError("generation object closure exceeds its count budget")
            if sum(item.byte_count for item in records) > self.limits.max_total_object_bytes:
                raise StoreError("generation object closure exceeds its aggregate byte budget")
            hook(3)

            staging_objects = self.objects.stage_missing(records, payloads)
            hook(4)

            self.objects.publish(staging_objects)
            staging_objects = ()
            for record in records:
                self.objects.verify(record)
            hook(5)

            objects_bytes = _records_bytes(records)
            generation_id = generation_digest(manifest_bytes, objects_bytes)
            staging_generation = self.layout.generations / f".stage-{uuid.uuid4().hex}"
            try:
                staging_generation.mkdir(mode=0o700)
            except OSError as exc:
                raise StoreError("generation staging directory cannot be created") from exc
            write_new_file(staging_generation / "manifest.json", manifest_bytes)
            write_new_file(staging_generation / "objects.json", objects_bytes)
            committed = Generation(
                generation_id,
                captured,
                records,
                manifest_bytes,
                objects_bytes,
            )
            hook(6)

            verified = _load_generation_from_directory(
                staging_generation,
                generation_id,
                self.limits,
                verify_objects=self.objects,
            )
            if verified != committed:
                raise StoreError("staged generation differs after disk verification")
            hook(7)

            for carrier in ("manifest.json", "objects.json"):
                descriptor = os.open(
                    staging_generation / carrier,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            fsync_directory(staging_generation)
            hook(8)

            destination = self.layout.generation_path(generation_id)
            _publish_generation(staging_generation, destination, committed)
            staging_generation = None
            hook(9)

            fsync_directory(self.layout.generations)
            hook(10)

            _commit_head(self.layout, generation_id)
            hook(11)
        finally:
            self.objects.discard_staged(staging_objects)
            if staging_generation is not None:
                try:
                    shutil.rmtree(staging_generation)
                except OSError:
                    pass
            lock.release()
        hook(12)
        if committed is None:
            raise StoreError("save ended without a committed generation")
        return committed


def create_project(
    root: Path,
    document: DocumentState,
    *,
    limits: ProjectLimits,
    object_payloads: Mapping[ObjectId, bytes],
    fault_hook: FaultHook | None = None,
) -> tuple[ProjectLayout, Generation]:
    layout = ProjectLayout.create(root, document.document_id)
    generation = GenerationStore(layout, limits).save(
        document,
        object_payloads=object_payloads,
        expected_head=None,
        fault_hook=fault_hook,
    )
    return layout, generation
