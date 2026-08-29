"""Safe project paths and the versioned on-disk layout."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kilix_image_shop.domain.identifiers import DocumentId, ObjectId


PROJECT_META_SCHEMA = "kilix.imageshop.project-meta/v1"
CONTROLLED_ROOT_ENTRIES = frozenset(
    {"HEAD", "LOCK", "objects", "generations", "autosave", "project-meta.json"}
)


class StoreError(RuntimeError):
    """A project carrier is unsafe, malformed, corrupt, or over budget."""


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StoreError(f"{field} must be a finite positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ProjectLimits:
    """Required finite limits for project parsing and reachability walks."""

    max_manifest_bytes: int
    max_objects: int
    max_object_bytes: int
    max_total_object_bytes: int
    max_layers: int
    max_group_depth: int

    def __post_init__(self) -> None:
        for field in (
            "max_manifest_bytes",
            "max_objects",
            "max_object_bytes",
            "max_total_object_bytes",
            "max_layers",
            "max_group_depth",
        ):
            _positive_integer(getattr(self, field), field)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one strict canonical JSON carrier with a final LF."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise StoreError("value cannot be serialized as canonical JSON") from exc


def _without_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StoreError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise StoreError(f"non-finite JSON number is forbidden: {value}")


def parse_canonical_json(payload: bytes, *, maximum_bytes: int) -> object:
    """Parse strict UTF-8 JSON and require the frozen canonical byte form."""

    _positive_integer(maximum_bytes, "JSON byte budget")
    if not isinstance(payload, bytes):
        raise StoreError("JSON carrier must be immutable bytes")
    if len(payload) > maximum_bytes:
        raise StoreError("JSON carrier exceeds its byte budget")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_without_duplicate_members,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("JSON carrier is not strict UTF-8 JSON") from exc
    if canonical_json_bytes(value) != payload:
        raise StoreError("JSON carrier is not in canonical form")
    return value


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise StoreError("project carrier cannot be inspected") from exc


def require_directory(path: Path) -> None:
    mode = _lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StoreError("project carrier is not a real directory")


def require_regular_file(path: Path) -> None:
    mode = _lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise StoreError("project carrier is not a real regular file")


def read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read a bounded regular file without following its final symlink."""

    _positive_integer(maximum_bytes, "file byte budget")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoreError("project carrier cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StoreError("project carrier is not a regular file")
        if metadata.st_size > maximum_bytes:
            raise StoreError("project carrier exceeds its byte budget")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        if len(payload) > maximum_bytes:
            raise StoreError("project carrier exceeds its byte budget")
        return bytes(payload)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoreError("project directory cannot be opened for synchronization") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise StoreError("project directory cannot be synchronized") from exc
    finally:
        os.close(descriptor)


def write_new_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Create, fully write, and fsync a new regular file."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, mode)
    except OSError as exc:
        raise StoreError("new project carrier cannot be created safely") from exc
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise StoreError("new project carrier could not be fully written")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise StoreError("new project carrier could not be persisted") from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    """Resolved paths beneath one already-verified project directory."""

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise StoreError("project root must be an absolute path")

    @property
    def head(self) -> Path:
        return self.root / "HEAD"

    @property
    def lock(self) -> Path:
        return self.root / "LOCK"

    @property
    def objects(self) -> Path:
        return self.root / "objects" / "sha256"

    @property
    def generations(self) -> Path:
        return self.root / "generations"

    @property
    def autosave(self) -> Path:
        return self.root / "autosave"

    @property
    def metadata(self) -> Path:
        return self.root / "project-meta.json"

    def object_path(self, object_id: ObjectId) -> Path:
        if not isinstance(object_id, ObjectId):
            raise StoreError("object path requires a typed content identity")
        return self.objects / object_id.value[:2] / object_id.value[2:]

    def generation_path(self, generation_id: ObjectId) -> Path:
        if not isinstance(generation_id, ObjectId):
            raise StoreError("generation path requires a typed content identity")
        return self.generations / generation_id.value

    @classmethod
    def create(cls, root: Path, document_id: DocumentId) -> ProjectLayout:
        """Create an empty local project root without inventing an initial HEAD."""

        if not isinstance(root, Path) or not root.is_absolute():
            raise StoreError("project root must be an absolute path")
        if not isinstance(document_id, DocumentId):
            raise StoreError("project metadata requires a typed document ID")
        try:
            root.mkdir(mode=0o700, parents=False, exist_ok=False)
            (root / "objects").mkdir(mode=0o700)
            (root / "objects" / "sha256").mkdir(mode=0o700)
            (root / "generations").mkdir(mode=0o700)
            (root / "autosave").mkdir(mode=0o700)
        except OSError as exc:
            raise StoreError("project layout cannot be created") from exc
        layout = cls(root)
        write_new_file(layout.lock, b"", mode=0o600)
        write_new_file(
            layout.metadata,
            canonical_json_bytes(
                {
                    "documentId": document_id.value,
                    "schema": PROJECT_META_SCHEMA,
                }
            ),
        )
        fsync_directory(root / "objects")
        fsync_directory(layout.objects)
        fsync_directory(layout.generations)
        fsync_directory(layout.autosave)
        fsync_directory(root)
        return layout

    def verify_structure(self, *, allow_missing_head: bool = False) -> None:
        require_directory(self.root)
        try:
            entries = {entry.name for entry in self.root.iterdir()}
        except OSError as exc:
            raise StoreError("project root cannot be enumerated") from exc
        expected = set(CONTROLLED_ROOT_ENTRIES)
        if allow_missing_head:
            expected.remove("HEAD")
        if entries != expected:
            raise StoreError("project root has missing or uncontrolled entries")
        require_regular_file(self.lock)
        require_regular_file(self.metadata)
        if not allow_missing_head:
            require_regular_file(self.head)
        require_directory(self.root / "objects")
        require_directory(self.objects)
        require_directory(self.generations)
        require_directory(self.autosave)

    def read_metadata(self, *, maximum_bytes: int = 4096) -> DocumentId:
        value = parse_canonical_json(
            read_regular_file(self.metadata, maximum_bytes=maximum_bytes),
            maximum_bytes=maximum_bytes,
        )
        required = {"documentId", "schema"}
        if not isinstance(value, dict) or set(value) != required:
            raise StoreError("project metadata has missing or unknown fields")
        if value["schema"] != PROJECT_META_SCHEMA:
            raise StoreError("project metadata schema is unsupported")
        try:
            return DocumentId.parse(value["documentId"])
        except ValueError as exc:
            raise StoreError("project metadata document identity is malformed") from exc
