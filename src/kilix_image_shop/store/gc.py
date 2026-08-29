"""Previewed reachability collection with crash-safe quarantine."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from kilix_image_shop.domain.identifiers import ObjectId

from .generations import load_generation, parse_head
from .layout import (
    ProjectLayout,
    ProjectLimits,
    StoreError,
    canonical_json_bytes,
    fsync_directory,
    read_regular_file,
    require_directory,
    write_new_file,
)
from .locking import ProjectWriterLock
from .objects import ObjectRecord, ObjectStore


GC_QUARANTINE_SCHEMA = "kilix.imageshop.gc-quarantine/v1"


@dataclass(frozen=True, slots=True)
class RootSnapshot:
    current_head_bytes: bytes
    autosave_heads: tuple[tuple[str, bytes], ...]
    history_objects: tuple[ObjectId, ...]
    save_lease_objects: tuple[ObjectId, ...]


@dataclass(frozen=True, slots=True)
class GCPreview:
    layout: ProjectLayout
    roots: RootSnapshot
    retained_generations: tuple[ObjectId, ...]
    reachable_objects: tuple[ObjectId, ...]
    unreachable_objects: tuple[ObjectRecord, ...]


@dataclass(frozen=True, slots=True)
class GCResult:
    quarantine: Path | None
    moved_objects: tuple[ObjectRecord, ...]


def _autosave_heads(layout: ProjectLayout) -> tuple[tuple[str, bytes], ...]:
    require_directory(layout.autosave)
    values: list[tuple[str, bytes]] = []
    for slot in layout.autosave.iterdir():
        require_directory(slot)
        names = {entry.name for entry in slot.iterdir()}
        if names != {"HEAD"}:
            raise StoreError("autosave slot has missing or uncontrolled entries")
        values.append((slot.name, read_regular_file(slot / "HEAD", maximum_bytes=4096)))
    return tuple(sorted(values, key=lambda item: item[0]))


def _snapshot(
    layout: ProjectLayout,
    history_objects: tuple[ObjectId, ...],
    save_lease_objects: tuple[ObjectId, ...],
) -> RootSnapshot:
    if any(not isinstance(item, ObjectId) for item in (*history_objects, *save_lease_objects)):
        raise StoreError("GC roots must be typed content identities")
    current = read_regular_file(layout.head, maximum_bytes=65)
    parse_head(current)
    return RootSnapshot(
        current,
        _autosave_heads(layout),
        tuple(sorted(set(history_objects), key=lambda item: item.value)),
        tuple(sorted(set(save_lease_objects), key=lambda item: item.value)),
    )


def _generation_roots(snapshot: RootSnapshot) -> tuple[ObjectId, ...]:
    values = {parse_head(snapshot.current_head_bytes)}
    for _, payload in snapshot.autosave_heads:
        try:
            values.add(parse_head(payload))
        except StoreError:
            # Explicit recovery retention can preserve a corrupt former HEAD. It is
            # retained byte-for-byte but cannot name an object reachability root.
            continue
    return tuple(sorted(values, key=lambda item: item.value))


def preview_gc(
    layout: ProjectLayout,
    limits: ProjectLimits,
    *,
    history_objects: tuple[ObjectId, ...] = (),
    save_lease_objects: tuple[ObjectId, ...] = (),
) -> GCPreview:
    """Compute, but do not apply, one exact reachability collection."""

    roots = _snapshot(layout, history_objects, save_lease_objects)
    generations = _generation_roots(roots)
    reachable = set(roots.history_objects) | set(roots.save_lease_objects)
    for generation_id in generations:
        generation = load_generation(layout, generation_id, limits, verify_objects=True)
        reachable.update(record.object_id for record in generation.objects)
    records = ObjectStore(layout, limits).enumerate_records()
    unreachable = tuple(record for record in records if record.object_id not in reachable)
    return GCPreview(
        layout,
        roots,
        generations,
        tuple(sorted(reachable, key=lambda item: item.value)),
        unreachable,
    )


def apply_gc(preview: GCPreview, limits: ProjectLimits) -> GCResult:
    """Revalidate preview roots under the writer lock and move only unreachable objects."""

    if not isinstance(preview, GCPreview):
        raise StoreError("GC apply requires a typed preview")
    layout = preview.layout
    with ProjectWriterLock(layout):
        current = preview_gc(
            layout,
            limits,
            history_objects=preview.roots.history_objects,
            save_lease_objects=preview.roots.save_lease_objects,
        )
        if current != preview:
            raise StoreError("GC roots or reachability changed after preview")
        if not preview.unreachable_objects:
            return GCResult(None, ())
        quarantine = layout.generations / f".quarantine-{uuid.uuid4().hex}"
        object_root = quarantine / "objects" / "sha256"
        try:
            object_root.mkdir(mode=0o700, parents=True)
        except OSError as exc:
            raise StoreError("GC quarantine cannot be created") from exc
        moved: list[ObjectRecord] = []
        try:
            for record in preview.unreachable_objects:
                source = layout.object_path(record.object_id)
                destination_parent = object_root / record.object_id.value[:2]
                destination_parent.mkdir(mode=0o700, exist_ok=True)
                destination = destination_parent / record.object_id.value[2:]
                os.rename(source, destination)
                fsync_directory(source.parent)
                fsync_directory(destination_parent)
                moved.append(record)
            write_new_file(
                quarantine / "quarantine.json",
                canonical_json_bytes(
                    {
                        "objects": [record.to_data() for record in moved],
                        "schema": GC_QUARANTINE_SCHEMA,
                    }
                ),
            )
            fsync_directory(object_root)
            fsync_directory(quarantine / "objects")
            fsync_directory(quarantine)
            fsync_directory(layout.generations)
        except (OSError, StoreError) as exc:
            raise StoreError("GC quarantine publication did not complete") from exc
        return GCResult(quarantine, tuple(moved))


def retire_quarantine(layout: ProjectLayout, quarantine: Path) -> None:
    """Remove one previously published private quarantine on a later call."""

    if quarantine.parent != layout.generations or not quarantine.name.startswith(
        ".quarantine-"
    ):
        raise StoreError("GC retirement target is not a project quarantine")
    with ProjectWriterLock(layout):
        require_directory(quarantine)
        try:
            shutil.rmtree(quarantine)
        except OSError as exc:
            raise StoreError("GC quarantine cannot be retired") from exc
        fsync_directory(layout.generations)
