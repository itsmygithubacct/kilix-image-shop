"""Synthetic operation registry; never an installed or release authority."""

from __future__ import annotations

from kilix_image_shop.domain.identifiers import ObjectId
from kilix_image_shop.engine.compatibility import (
    OperationDefinition,
    OperationProperty,
    OperationRegistry,
    PropertySource,
    REQUIRED_OPERATION_KEYS,
    RegistryFamily,
    RegistryValueKind,
)


def semantic_property(
    native_name: str,
    native_type: str,
    value_kind: RegistryValueKind,
    semantic_name: str,
    *,
    default: object = None,
) -> OperationProperty:
    return OperationProperty(
        native_name=native_name,
        native_type=native_type,
        value_kind=value_kind,
        source=PropertySource.SEMANTIC,
        default_value=default,
        semantic_name=semantic_name,
    )


def fixed_property(
    native_name: str,
    native_type: str,
    value_kind: RegistryValueKind,
    value: object,
    *,
    default: object,
) -> OperationProperty:
    return OperationProperty(
        native_name=native_name,
        native_type=native_type,
        value_kind=value_kind,
        source=PropertySource.FIXED,
        default_value=default,
        fixed_value=value,
    )


def default_property(
    native_name: str,
    native_type: str,
    value_kind: RegistryValueKind,
    default: object,
) -> OperationProperty:
    return OperationProperty(
        native_name=native_name,
        native_type=native_type,
        value_kind=value_kind,
        source=PropertySource.DEFAULT,
        default_value=default,
    )


def family_for(key: str) -> RegistryFamily:
    if key.startswith("transform."):
        return RegistryFamily.AFFINE_RESAMPLING
    if key.startswith(("blend.", "group.")):
        return RegistryFamily.OPACITY_BLEND
    if key.startswith("mask."):
        return RegistryFamily.MASK_APPLICATION
    if key.startswith("adjustment."):
        return RegistryFamily.ADJUSTMENT
    if key.startswith("colour."):
        return RegistryFamily.ICC_CONVERSION
    if key == "source.text-raster":
        return RegistryFamily.TEXT_RASTER
    if key.startswith(("source.", "sink.", "import.", "export.")):
        return RegistryFamily.IMPORT_EXPORT
    return RegistryFamily.DESTINATION


def operation_for(key: str) -> str:
    exact = {
        "source.pixel": "gegl:buffer-source",
        "source.text-raster": "gegl:buffer-source",
        "sink.write-buffer": "gegl:write-buffer",
        "transform.affine": "gegl:transform",
        "transform.crop": "gegl:crop",
        "blend.opacity": "gegl:opacity",
        "mask.apply": "gegl:opacity",
        "mask.invert": "gegl:invert-linear",
        "group.compose": "svg:src-over",
        "colour.cast": "gegl:cast-space",
        "colour.convert": "gegl:convert-space",
        "destination.scale": "gegl:scale-ratio",
        "destination.crop": "gegl:crop",
        "adjustment.contrast": "gegl:brightness-contrast",
        "adjustment.saturation": "gegl:saturation",
        "import.jpeg": "gegl:load",
        "export.jpeg": "gegl:jpg-save",
        "export.png": "gegl:png-save",
        "export.tiff": "gegl:tiff-save",
        "export.webp": "gegl:webp-save",
    }
    if key in exact:
        return exact[key]
    if key.startswith("blend.mode."):
        mode = key.removeprefix("blend.mode.")
        return "svg:src-over" if mode == "normal" else f"svg:{mode}"
    if key.startswith("adjustment."):
        return "gegl:brightness-contrast"
    if key.startswith("import."):
        return "gegl:load"
    return "gegl:buffer-source"


def properties_for(key: str) -> tuple[OperationProperty, ...]:
    if key in {"source.pixel", "source.text-raster"}:
        return (
            semantic_property(
                "buffer",
                "GeglBuffer",
                RegistryValueKind.BUFFER,
                "buffer",
            ),
        )
    if key == "sink.write-buffer":
        return (
            semantic_property(
                "buffer",
                "GeglBuffer",
                RegistryValueKind.BUFFER,
                "buffer",
            ),
        )
    if key == "transform.affine":
        return (
            semantic_property(
                "transform",
                "gchararray",
                RegistryValueKind.STRING,
                "transform",
            ),
        )
    if key in {"transform.crop", "destination.crop"}:
        return tuple(
            sorted(
                (
                    semantic_property("height", "gdouble", RegistryValueKind.NUMBER, "height"),
                    fixed_property(
                        "reset-origin",
                        "gboolean",
                        RegistryValueKind.BOOLEAN,
                        False,
                        default=False,
                    ),
                    semantic_property("width", "gdouble", RegistryValueKind.NUMBER, "width"),
                    semantic_property("x", "gdouble", RegistryValueKind.NUMBER, "x"),
                    semantic_property("y", "gdouble", RegistryValueKind.NUMBER, "y"),
                ),
                key=lambda item: item.native_name,
            )
        )
    if key in {"blend.opacity", "mask.apply"}:
        return (
            semantic_property(
                "value",
                "gdouble",
                RegistryValueKind.NUMBER,
                "value",
                default=1.0,
            ),
        )
    if key.startswith("blend.mode.") or key == "group.compose":
        return (
            fixed_property(
                "srgb",
                "gboolean",
                RegistryValueKind.BOOLEAN,
                False,
                default=False,
            ),
        )
    if key == "adjustment.contrast":
        return tuple(
            sorted(
                (
                    semantic_property(
                        "contrast",
                        "gdouble",
                        RegistryValueKind.NUMBER,
                        "amount",
                        default=1.0,
                    ),
                    fixed_property(
                        "brightness",
                        "gdouble",
                        RegistryValueKind.NUMBER,
                        0.0,
                        default=0.0,
                    ),
                ),
                key=lambda item: item.native_name,
            )
        )
    if key.startswith("adjustment."):
        return ()
    if key in {"colour.cast", "colour.convert"}:
        return tuple(
            sorted(
                (
                    semantic_property(
                        "path",
                        "gchararray",
                        RegistryValueKind.PROFILE_PATH,
                        "profile",
                    ),
                    default_property(
                        "pointer", "gpointer", RegistryValueKind.STRING, None
                    ),
                    default_property(
                        "space-name", "gchararray", RegistryValueKind.STRING, None
                    ),
                ),
                key=lambda item: item.native_name,
            )
        )
    if key == "destination.scale":
        return (
            semantic_property("x", "gdouble", RegistryValueKind.NUMBER, "x"),
            semantic_property("y", "gdouble", RegistryValueKind.NUMBER, "y"),
        )
    return ()


def pads_for(key: str) -> tuple[tuple[str, ...], str | None]:
    if key.startswith(("source.", "import.")):
        return (), "output"
    if key.startswith(("sink.", "export.")):
        return ("input",), None
    if key.startswith("blend.mode.") or key in {"group.compose", "mask.apply"}:
        return ("input", "aux"), "output"
    return ("input",), "output"


def synthetic_registry() -> OperationRegistry:
    definitions: list[OperationDefinition] = []
    for key in REQUIRED_OPERATION_KEYS:
        input_pads, output_pad = pads_for(key)
        definitions.append(
            OperationDefinition(
                semantic_key=key,
                family=family_for(key),
                operation=operation_for(key),
                properties=properties_for(key),
                input_pads=input_pads,
                output_pad=output_pad,
                halo_pixels=1 if key == "adjustment.contrast" else 0,
                golden_fixture_digest=ObjectId.from_bytes(
                    f"synthetic golden {key}\n".encode()
                ),
            )
        )
    return OperationRegistry(OperationRegistry.SCHEMA, tuple(definitions))
