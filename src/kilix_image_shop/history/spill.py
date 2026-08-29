"""Private XDG-cache history spill with atomic publication and digest checks."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from kilix_image_shop.domain.identifiers import DocumentId, ObjectId


class SpillError(RuntimeError):
    """A spill carrier is absent, unsafe, corrupt, or over budget."""


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SpillError("history spill directory cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SpillError("history spill carrier is not a real directory")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise SpillError("history spill directory is not private to its owner")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SpillError("history spill directory cannot be synchronized") from exc


def _write_new(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SpillError("history spill staging file cannot be created") from exc
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise SpillError("history spill staging file was not fully written")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise SpillError("history spill staging file cannot be persisted") from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class SpillRef:
    entry_id: ObjectId
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, ObjectId):
            raise SpillError("spill reference requires a typed digest")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count <= 0
        ):
            raise SpillError("spill reference requires a positive byte count")


@dataclass(frozen=True, slots=True)
class SpillStore:
    root: Path
    max_record_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise SpillError("history spill root must be an absolute path")
        if (
            isinstance(self.max_record_bytes, bool)
            or not isinstance(self.max_record_bytes, int)
            or self.max_record_bytes <= 0
        ):
            raise SpillError("history spill record budget must be positive")
        _require_private_directory(self.root)

    @classmethod
    def create(
        cls,
        xdg_cache_home: Path,
        document_id: DocumentId,
        *,
        max_record_bytes: int,
    ) -> SpillStore:
        """Create one owner-private document spill below an explicit XDG cache."""

        if not isinstance(xdg_cache_home, Path) or not xdg_cache_home.is_absolute():
            raise SpillError("XDG cache root must be an absolute path")
        if not isinstance(document_id, DocumentId):
            raise SpillError("history spill requires a typed document identity")
        _require_private_directory(xdg_cache_home)
        current = xdg_cache_home
        for component in ("kilix-image-shop", "history", document_id.value):
            current = current / component
            try:
                current.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise SpillError("history spill directory cannot be created") from exc
            _require_private_directory(current)
        _fsync_directory(current.parent)
        return cls(current, max_record_bytes)

    def path_for(self, entry_id: ObjectId) -> Path:
        if not isinstance(entry_id, ObjectId):
            raise SpillError("spill path requires a typed digest")
        return self.root / f"{entry_id.value}.json"

    def put(self, entry_id: ObjectId, payload: bytes) -> SpillRef:
        if not isinstance(payload, bytes) or not payload:
            raise SpillError("history spill payload must be non-empty immutable bytes")
        if len(payload) > self.max_record_bytes:
            raise SpillError("history spill record exceeds its byte budget")
        if ObjectId.from_bytes(payload) != entry_id:
            raise SpillError("history spill payload differs from its entry identity")
        _require_private_directory(self.root)
        destination = self.path_for(entry_id)
        temporary = self.root / f".stage-{uuid.uuid4().hex}"
        try:
            _write_new(temporary, payload)
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                existing = self.get(SpillRef(entry_id, len(payload)))
                if existing != payload:
                    raise SpillError("existing spill identity has different bytes")
            except OSError as exc:
                raise SpillError("history spill record cannot be atomically published") from exc
            temporary.unlink()
            _fsync_directory(self.root)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return SpillRef(entry_id, len(payload))

    def get(self, reference: SpillRef) -> bytes:
        if not isinstance(reference, SpillRef):
            raise SpillError("history spill read requires a typed reference")
        if reference.byte_count > self.max_record_bytes:
            raise SpillError("history spill reference exceeds its byte budget")
        path = self.path_for(reference.entry_id)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SpillError("history spill record is missing or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != reference.byte_count:
                raise SpillError("history spill record size or type differs")
            payload = bytearray()
            while len(payload) < reference.byte_count:
                block = os.read(descriptor, reference.byte_count - len(payload))
                if not block:
                    break
                payload.extend(block)
            if len(payload) != reference.byte_count or os.read(descriptor, 1):
                raise SpillError("history spill record is truncated or oversized")
        finally:
            os.close(descriptor)
        result = bytes(payload)
        if hashlib.sha256(result).hexdigest() != reference.entry_id.value:
            raise SpillError("history spill record digest differs")
        return result

    def delete(self, reference: SpillRef) -> None:
        if not isinstance(reference, SpillRef):
            raise SpillError("history spill deletion requires a typed reference")
        try:
            self.path_for(reference.entry_id).unlink(missing_ok=True)
        except OSError as exc:
            raise SpillError("history spill record cannot be removed") from exc
        _fsync_directory(self.root)
