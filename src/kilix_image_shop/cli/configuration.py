"""Explicit finite command-line configuration; no ceiling has a hidden default."""

from __future__ import annotations

from enum import IntEnum

from kilix_image_shop.store.layout import ProjectLimits, StoreError


PROGRAM_NAME = "kilix-image-shop"

# Every ceiling below is a command-line default, not an owner-frozen release
# tier. The frozen 100-megapixel tier and its working format remain owner-owned
# and are reported as blocked rather than assumed here.
DEFAULT_MAX_MANIFEST_BYTES = 8_388_608
DEFAULT_MAX_OBJECTS = 65_536
DEFAULT_MAX_OBJECT_BYTES = 1_073_741_824
DEFAULT_MAX_TOTAL_OBJECT_BYTES = 8_589_934_592
DEFAULT_MAX_LAYERS = 4_096
DEFAULT_MAX_GROUP_DEPTH = 32
DEFAULT_MAX_SIDECAR_BYTES = 1_048_576
DEFAULT_MAX_PRESET_BYTES = 1_048_576
DEFAULT_MAX_ACTIVE_OPERATIONS = 2
DEFAULT_MAX_RETAINED_OPERATIONS = 8

# The contained-GUI toolkit is an owner decision. I1 selects none, and the
# command-line surface must never imply one exists.
GUI_TOOLKIT_SELECTION: str | None = None


class ExitCode(IntEnum):
    """Closed exit-status set; every command returns exactly one of these."""

    OK = 0
    INTERNAL = 1
    USAGE = 2
    UNAVAILABLE = 3
    INVALID_DATA = 4


def default_project_limits() -> ProjectLimits:
    """Return the finite command-line project ceilings as one immutable value."""

    return ProjectLimits(
        max_manifest_bytes=DEFAULT_MAX_MANIFEST_BYTES,
        max_objects=DEFAULT_MAX_OBJECTS,
        max_object_bytes=DEFAULT_MAX_OBJECT_BYTES,
        max_total_object_bytes=DEFAULT_MAX_TOTAL_OBJECT_BYTES,
        max_layers=DEFAULT_MAX_LAYERS,
        max_group_depth=DEFAULT_MAX_GROUP_DEPTH,
    )


def project_limits(
    *,
    max_manifest_bytes: int | None = None,
    max_objects: int | None = None,
    max_object_bytes: int | None = None,
    max_total_object_bytes: int | None = None,
    max_layers: int | None = None,
    max_group_depth: int | None = None,
) -> ProjectLimits:
    """Apply explicit overrides to the finite defaults, refusing an open ceiling."""

    default = default_project_limits()
    values = {
        "max_manifest_bytes": max_manifest_bytes,
        "max_objects": max_objects,
        "max_object_bytes": max_object_bytes,
        "max_total_object_bytes": max_total_object_bytes,
        "max_layers": max_layers,
        "max_group_depth": max_group_depth,
    }
    resolved: dict[str, int] = {}
    for name, value in values.items():
        if value is None:
            resolved[name] = getattr(default, name)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StoreError(f"{name} must be a finite positive integer")
        resolved[name] = value
    return ProjectLimits(**resolved)


def limit_rows(limits: ProjectLimits) -> tuple[tuple[str, str], ...]:
    """Render the active ceilings so no command hides the budget it enforced."""

    return (
        ("limits.maxManifestBytes", str(limits.max_manifest_bytes)),
        ("limits.maxObjects", str(limits.max_objects)),
        ("limits.maxObjectBytes", str(limits.max_object_bytes)),
        ("limits.maxTotalObjectBytes", str(limits.max_total_object_bytes)),
        ("limits.maxLayers", str(limits.max_layers)),
        ("limits.maxGroupDepth", str(limits.max_group_depth)),
    )


def limit_data(limits: ProjectLimits) -> dict[str, object]:
    return {
        "maxManifestBytes": limits.max_manifest_bytes,
        "maxObjects": limits.max_objects,
        "maxObjectBytes": limits.max_object_bytes,
        "maxTotalObjectBytes": limits.max_total_object_bytes,
        "maxLayers": limits.max_layers,
        "maxGroupDepth": limits.max_group_depth,
    }


__all__ = (
    "DEFAULT_MAX_ACTIVE_OPERATIONS",
    "DEFAULT_MAX_GROUP_DEPTH",
    "DEFAULT_MAX_LAYERS",
    "DEFAULT_MAX_MANIFEST_BYTES",
    "DEFAULT_MAX_OBJECTS",
    "DEFAULT_MAX_OBJECT_BYTES",
    "DEFAULT_MAX_PRESET_BYTES",
    "DEFAULT_MAX_RETAINED_OPERATIONS",
    "DEFAULT_MAX_SIDECAR_BYTES",
    "DEFAULT_MAX_TOTAL_OBJECT_BYTES",
    "ExitCode",
    "GUI_TOOLKIT_SELECTION",
    "PROGRAM_NAME",
    "default_project_limits",
    "limit_data",
    "limit_rows",
    "project_limits",
)
