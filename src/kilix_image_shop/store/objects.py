"""Content-addressed immutable object reads and writes."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from kilix_image_shop.domain.identifiers import ObjectId

from .layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    fsync_directory,
    read_regular_file,
    require_directory,
    write_new_file,
)


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    object_id: ObjectId
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, ObjectId):
            raise StoreError("object record requires a typed content identity")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise StoreError("object record byte count must be a non-negative integer")

    def to_data(self) -> dict[str, object]:
        return {"byteCount": self.byte_count, "sha256": self.object_id.value}


@dataclass(frozen=True, slots=True)
class StagedObject:
    record: ObjectRecord
    temporary_path: Path
    destination_path: Path


class ObjectStore:
    def __init__(self, layout: ProjectLayout, limits: ProjectLimits) -> None:
        if not isinstance(layout, ProjectLayout) or not isinstance(limits, ProjectLimits):
            raise StoreError("object store requires typed layout and limits")
        self.layout = layout
        self.limits = limits

    def _verify_parent(self, object_id: ObjectId, *, create: bool) -> Path:
        require_directory(self.layout.objects)
        parent = self.layout.objects / object_id.value[:2]
        if create:
            try:
                parent.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise StoreError("object shard cannot be created") from exc
        require_directory(parent)
        return parent

    def contains(self, record: ObjectRecord) -> bool:
        path = self.layout.object_path(record.object_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError("object carrier cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StoreError("object carrier is not a real regular file")
        self.verify(record)
        return True

    def read(self, record: ObjectRecord) -> bytes:
        if record.byte_count > self.limits.max_object_bytes:
            raise StoreError("object exceeds the per-object byte budget")
        self._verify_parent(record.object_id, create=False)
        payload = read_regular_file(
            self.layout.object_path(record.object_id),
            maximum_bytes=self.limits.max_object_bytes,
        )
        if len(payload) != record.byte_count:
            raise StoreError("object byte count differs from its record")
        if hashlib.sha256(payload).hexdigest() != record.object_id.value:
            raise StoreError("object digest differs from its path identity")
        return payload

    def verify(self, record: ObjectRecord) -> None:
        self.read(record)

    def stage_missing(
        self,
        records: tuple[ObjectRecord, ...],
        payloads: Mapping[ObjectId, bytes],
    ) -> tuple[StagedObject, ...]:
        if len(records) > self.limits.max_objects:
            raise StoreError("object closure exceeds its count budget")
        total = sum(record.byte_count for record in records)
        if total > self.limits.max_total_object_bytes:
            raise StoreError("object closure exceeds its aggregate byte budget")
        staged: list[StagedObject] = []
        try:
            for record in records:
                if record.byte_count > self.limits.max_object_bytes:
                    raise StoreError("object exceeds the per-object byte budget")
                destination = self.layout.object_path(record.object_id)
                try:
                    exists = destination.exists()
                except OSError as exc:
                    raise StoreError("object destination cannot be inspected") from exc
                if exists:
                    self.verify(record)
                    continue
                payload = payloads.get(record.object_id)
                if not isinstance(payload, bytes):
                    raise StoreError("missing object has no immutable payload")
                if len(payload) != record.byte_count:
                    raise StoreError("object payload differs from its declared byte count")
                if ObjectId.from_bytes(payload) != record.object_id:
                    raise StoreError("object payload differs from its declared digest")
                parent = self._verify_parent(record.object_id, create=True)
                temporary = parent / f".stage-{uuid.uuid4().hex}"
                write_new_file(temporary, payload)
                staged.append(StagedObject(record, temporary, destination))
            return tuple(staged)
        except Exception:
            self.discard_staged(tuple(staged))
            raise

    def publish(self, staged: tuple[StagedObject, ...]) -> None:
        synchronized: set[Path] = set()
        for item in staged:
            payload = read_regular_file(
                item.temporary_path,
                maximum_bytes=self.limits.max_object_bytes,
            )
            if len(payload) != item.record.byte_count or ObjectId.from_bytes(payload) != item.record.object_id:
                raise StoreError("staged object failed digest or size verification")
            try:
                os.link(
                    item.temporary_path,
                    item.destination_path,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self.verify(item.record)
            except OSError as exc:
                raise StoreError("immutable object cannot be atomically published") from exc
            try:
                item.temporary_path.unlink()
            except OSError as exc:
                raise StoreError("published object staging carrier cannot be removed") from exc
            synchronized.add(item.destination_path.parent)
        for parent in synchronized:
            fsync_directory(parent)
        if synchronized:
            fsync_directory(self.layout.objects)

    @staticmethod
    def discard_staged(staged: tuple[StagedObject, ...]) -> None:
        for item in staged:
            try:
                item.temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def enumerate_records(self) -> tuple[ObjectRecord, ...]:
        require_directory(self.layout.objects)
        records: list[ObjectRecord] = []
        try:
            shards = tuple(self.layout.objects.iterdir())
        except OSError as exc:
            raise StoreError("object store cannot be enumerated") from exc
        for shard in shards:
            if shard.name.startswith(".stage-"):
                continue
            if len(shard.name) != 2 or any(character not in "0123456789abcdef" for character in shard.name):
                raise StoreError("object store contains an uncontrolled shard")
            require_directory(shard)
            for path in shard.iterdir():
                if path.name.startswith(".stage-"):
                    continue
                digest = shard.name + path.name
                try:
                    object_id = ObjectId(digest)
                except ValueError as exc:
                    raise StoreError("object store contains a malformed path") from exc
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise StoreError("object store contains a non-regular carrier")
                records.append(ObjectRecord(object_id, metadata.st_size))
        if len(records) > self.limits.max_objects:
            raise StoreError("object store exceeds its count budget")
        if sum(record.byte_count for record in records) > self.limits.max_total_object_bytes:
            raise StoreError("object store exceeds its aggregate byte budget")
        return tuple(sorted(records, key=lambda item: item.object_id.value))
