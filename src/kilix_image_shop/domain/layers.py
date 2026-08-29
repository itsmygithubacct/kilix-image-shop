"""Closed immutable layer, mask, selection, and provenance values."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from .geometry import AffineTransform, Rect, _coordinate, _positive
from .identifiers import DomainValidationError, LayerId, ObjectId


class BlendMode(StrEnum):
    NORMAL = "normal"
    MULTIPLY = "multiply"
    SCREEN = "screen"
    OVERLAY = "overlay"
    DARKEN = "darken"
    LIGHTEN = "lighten"
    COLOR_DODGE = "color-dodge"
    COLOR_BURN = "color-burn"
    HARD_LIGHT = "hard-light"
    SOFT_LIGHT = "soft-light"
    DIFFERENCE = "difference"
    EXCLUSION = "exclusion"
    HUE = "hue"
    SATURATION = "saturation"
    COLOR = "color"
    LUMINOSITY = "luminosity"


class AdjustmentId(StrEnum):
    EXPOSURE = "exposure"
    CONTRAST = "contrast"
    CURVES = "curves"
    LEVELS = "levels"
    WHITE_BALANCE = "white-balance"
    SATURATION = "saturation"
    HUE = "hue"
    SHARPEN = "sharpen"
    BLUR = "blur"


class MaskSource(StrEnum):
    HAND_PAINTED = "hand-painted"
    SELECTION = "selection"
    OPERATION = "operation"


class SelectionKind(StrEnum):
    VECTOR = "vector"
    RASTER = "raster"


class TextAlignment(StrEnum):
    START = "start"
    CENTER = "center"
    END = "end"
    JUSTIFY = "justify"


_KEY_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_CODE_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+\Z")
_FONT_AXIS_RE = re.compile(r"[ -~]{4}\Z")


def _text(
    value: object,
    field: str,
    *,
    maximum: int,
    allow_multiline: bool = False,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DomainValidationError(f"{field} must be a non-empty bounded string")
    allowed_controls = {"\t", "\n", "\r"} if allow_multiline else set()
    if any(
        (ord(character) < 0x20 and character not in allowed_controls)
        or ord(character) == 0x7F
        for character in value
    ):
        raise DomainValidationError(f"{field} contains a forbidden control character")
    return value


def _enum(enum_type: type[StrEnum], value: object, field: str) -> StrEnum:
    if not isinstance(value, str):
        raise DomainValidationError(f"{field} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DomainValidationError(f"unsupported {field}: {value!r}") from exc


def _u16(value: object, field: str = "opacity") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise DomainValidationError(f"{field} must be an integer in [0, 65535]")
    return value


ParameterValue: TypeAlias = bool | int | float | str | tuple[float, ...]


def _parameter_value(value: object) -> ParameterValue:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError("parameter number must be finite")
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return _text(value, "parameter string", maximum=4096)
    if isinstance(value, (list, tuple)):
        if not value or len(value) > 4096:
            raise DomainValidationError("numeric parameter vector is empty or too large")
        normalized: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise DomainValidationError("parameter vector must contain only numbers")
            parsed = float(item)
            if not math.isfinite(parsed):
                raise DomainValidationError("parameter vector number must be finite")
            normalized.append(0.0 if parsed == 0.0 else parsed)
        return tuple(normalized)
    raise DomainValidationError("unsupported parameter value")


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    value: ParameterValue

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _KEY_RE.fullmatch(self.name) is None:
            raise DomainValidationError("parameter name is not canonical")
        object.__setattr__(self, "value", _parameter_value(self.value))

    @classmethod
    def from_data(cls, value: object) -> Parameter:
        if not isinstance(value, dict) or set(value) != {"name", "value"}:
            raise DomainValidationError("parameter has missing or unknown fields")
        return cls(name=value["name"], value=value["value"])

    def to_data(self) -> dict[str, object]:
        value: object = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"name": self.name, "value": value}


def _parameters(values: tuple[Parameter, ...], field: str) -> tuple[Parameter, ...]:
    if not isinstance(values, tuple):
        raise DomainValidationError(f"{field} must be an immutable tuple")
    if any(not isinstance(item, Parameter) for item in values):
        raise DomainValidationError(f"{field} contains an untyped parameter")
    names = [item.name for item in values]
    if len(names) != len(set(names)):
        raise DomainValidationError(f"{field} contains duplicate names")
    normalized = tuple(sorted(values, key=lambda item: item.name))
    return normalized


@dataclass(frozen=True, slots=True)
class Adjustment:
    adjustment_id: AdjustmentId
    parameters: tuple[Parameter, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.adjustment_id, AdjustmentId):
            raise DomainValidationError("adjustment ID must be closed")
        if any(not isinstance(item, Parameter) for item in self.parameters):
            raise DomainValidationError("adjustment contains an untyped parameter")
        object.__setattr__(self, "parameters", _parameters(self.parameters, "adjustment"))

    @classmethod
    def from_data(cls, value: object) -> Adjustment:
        if not isinstance(value, dict) or set(value) != {"id", "parameters"}:
            raise DomainValidationError("adjustment has missing or unknown fields")
        raw_parameters = value["parameters"]
        if not isinstance(raw_parameters, list):
            raise DomainValidationError("adjustment parameters must be a list")
        return cls(
            adjustment_id=_enum(AdjustmentId, value["id"], "adjustment ID"),
            parameters=tuple(Parameter.from_data(item) for item in raw_parameters),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "id": self.adjustment_id.value,
            "parameters": [item.to_data() for item in self.parameters],
        }


@dataclass(frozen=True, slots=True)
class OperationProvenance:
    schema: str
    operation: str
    provider: str
    model_digest: ObjectId | None
    runtime_digest: ObjectId
    prompt: str | None
    seed: int | None
    parameters: tuple[Parameter, ...]
    source_layer_digest: ObjectId | None
    occurred_at: str

    SCHEMA = "kilix.imageshop.operation-provenance/v1"

    def __post_init__(self) -> None:
        if self.schema != self.SCHEMA:
            raise DomainValidationError("unsupported operation provenance schema")
        if not isinstance(self.operation, str) or _CODE_RE.fullmatch(self.operation) is None:
            raise DomainValidationError("invalid operation identity")
        if not isinstance(self.provider, str) or _CODE_RE.fullmatch(self.provider) is None:
            raise DomainValidationError("invalid provider identity")
        if self.model_digest is not None and not isinstance(self.model_digest, ObjectId):
            raise DomainValidationError("model identity must be content-addressed")
        if not isinstance(self.runtime_digest, ObjectId):
            raise DomainValidationError("runtime identity must be content-addressed")
        if self.source_layer_digest is not None and not isinstance(
            self.source_layer_digest, ObjectId
        ):
            raise DomainValidationError("source-layer identity must be content-addressed")
        if self.prompt is not None:
            _text(
                self.prompt,
                "operation prompt",
                maximum=131072,
                allow_multiline=True,
            )
        if self.seed is not None and (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= 2**64 - 1
        ):
            raise DomainValidationError("operation seed is outside uint64")
        object.__setattr__(
            self, "parameters", _parameters(self.parameters, "provenance parameters")
        )
        if not isinstance(self.occurred_at, str):
            raise DomainValidationError("operation time must be an RFC 3339 string")
        try:
            parsed = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainValidationError("operation time is not RFC 3339") from exc
        if parsed.tzinfo is None:
            raise DomainValidationError("operation time must carry an offset")
        if "T" not in self.occurred_at or parsed.isoformat() != self.occurred_at:
            raise DomainValidationError("operation time must use canonical RFC 3339 form")

    @classmethod
    def from_data(cls, value: object) -> OperationProvenance:
        required = {
            "schema",
            "operation",
            "provider",
            "modelSha256",
            "runtimeSha256",
            "prompt",
            "seed",
            "parameters",
            "sourceLayerSha256",
            "occurredAt",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError("operation provenance has missing or unknown fields")
        raw_parameters = value["parameters"]
        if not isinstance(raw_parameters, list):
            raise DomainValidationError("provenance parameters must be a list")
        return cls(
            schema=value["schema"],
            operation=value["operation"],
            provider=value["provider"],
            model_digest=(
                None
                if value["modelSha256"] is None
                else ObjectId.parse(value["modelSha256"])
            ),
            runtime_digest=ObjectId.parse(value["runtimeSha256"]),
            prompt=value["prompt"],
            seed=value["seed"],
            parameters=tuple(Parameter.from_data(item) for item in raw_parameters),
            source_layer_digest=(
                None
                if value["sourceLayerSha256"] is None
                else ObjectId.parse(value["sourceLayerSha256"])
            ),
            occurred_at=value["occurredAt"],
        )

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "provider": self.provider,
            "modelSha256": (
                None if self.model_digest is None else self.model_digest.value
            ),
            "runtimeSha256": self.runtime_digest.value,
            "prompt": self.prompt,
            "seed": self.seed,
            "parameters": [item.to_data() for item in self.parameters],
            "sourceLayerSha256": (
                None
                if self.source_layer_digest is None
                else self.source_layer_digest.value
            ),
            "occurredAt": self.occurred_at,
        }


@dataclass(frozen=True, slots=True)
class MaskObject:
    object_id: ObjectId
    width: int
    height: int
    origin_x: int
    origin_y: int
    source: MaskSource
    source_ref: ObjectId | None = None
    operation_provenance: OperationProvenance | None = None
    format: str = "Y u8"
    semantics: str = "foreground-alpha"

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, ObjectId):
            raise DomainValidationError("mask object must be content-addressed")
        _positive(self.width, "mask width")
        _positive(self.height, "mask height")
        _coordinate(self.origin_x, "mask origin_x")
        _coordinate(self.origin_y, "mask origin_y")
        if self.format != "Y u8" or self.semantics != "foreground-alpha":
            raise DomainValidationError("unsupported mask format or semantics")
        if not isinstance(self.source, MaskSource):
            raise DomainValidationError("mask source must be closed")
        if self.source_ref is not None and not isinstance(self.source_ref, ObjectId):
            raise DomainValidationError("mask source ref must be content-addressed")
        if self.operation_provenance is not None and not isinstance(
            self.operation_provenance, OperationProvenance
        ):
            raise DomainValidationError("mask operation provenance must be typed")
        if self.source is MaskSource.HAND_PAINTED:
            if self.source_ref is not None or self.operation_provenance is not None:
                raise DomainValidationError("hand-painted mask has incompatible provenance")
        elif self.source is MaskSource.SELECTION:
            if self.source_ref is None or self.operation_provenance is not None:
                raise DomainValidationError("selection mask requires only a source ref")
        elif self.operation_provenance is None or self.source_ref is not None:
            raise DomainValidationError(
                "operation mask requires only typed operation provenance"
            )

    @classmethod
    def from_data(cls, value: object) -> MaskObject:
        required = {
            "objectSha256",
            "width",
            "height",
            "originX",
            "originY",
            "format",
            "semantics",
            "source",
            "sourceRefSha256",
            "operationProvenance",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError("mask has missing or unknown fields")
        return cls(
            object_id=ObjectId.parse(value["objectSha256"]),
            width=value["width"],
            height=value["height"],
            origin_x=value["originX"],
            origin_y=value["originY"],
            format=value["format"],
            semantics=value["semantics"],
            source=_enum(MaskSource, value["source"], "mask source"),
            source_ref=(
                None
                if value["sourceRefSha256"] is None
                else ObjectId.parse(value["sourceRefSha256"])
            ),
            operation_provenance=(
                None
                if value["operationProvenance"] is None
                else OperationProvenance.from_data(value["operationProvenance"])
            ),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "objectSha256": self.object_id.value,
            "width": self.width,
            "height": self.height,
            "originX": self.origin_x,
            "originY": self.origin_y,
            "format": self.format,
            "semantics": self.semantics,
            "source": self.source.value,
            "sourceRefSha256": None if self.source_ref is None else self.source_ref.value,
            "operationProvenance": (
                None
                if self.operation_provenance is None
                else self.operation_provenance.to_data()
            ),
        }


def _layer_common(
    layer_id: LayerId,
    name: str,
    visible: bool,
    opacity_u16: int,
    blend_mode: BlendMode,
) -> None:
    if not isinstance(layer_id, LayerId):
        raise DomainValidationError("layer ID must be typed")
    _text(name, "layer name", maximum=256)
    if not isinstance(visible, bool):
        raise DomainValidationError("layer visibility must be boolean")
    _u16(opacity_u16)
    if not isinstance(blend_mode, BlendMode):
        raise DomainValidationError("layer blend mode must be closed")


@dataclass(frozen=True, slots=True, kw_only=True)
class PixelLayer:
    layer_id: LayerId
    name: str
    asset_digest: ObjectId
    visible: bool = True
    opacity_u16: int = 65535
    blend_mode: BlendMode = BlendMode.NORMAL
    transform: AffineTransform = AffineTransform()
    mask: MaskObject | None = None
    operation_provenance: OperationProvenance | None = None

    def __post_init__(self) -> None:
        _layer_common(
            self.layer_id, self.name, self.visible, self.opacity_u16, self.blend_mode
        )
        if not isinstance(self.asset_digest, ObjectId):
            raise DomainValidationError("pixel layer asset must be content-addressed")
        if not isinstance(self.transform, AffineTransform):
            raise DomainValidationError("pixel layer transform must be affine")
        if self.mask is not None and not isinstance(self.mask, MaskObject):
            raise DomainValidationError("pixel layer mask must be typed")
        if self.operation_provenance is not None and not isinstance(
            self.operation_provenance, OperationProvenance
        ):
            raise DomainValidationError("pixel layer provenance must be typed")


@dataclass(frozen=True, slots=True, kw_only=True)
class AdjustmentLayer:
    layer_id: LayerId
    name: str
    adjustment: Adjustment
    visible: bool = True
    opacity_u16: int = 65535
    blend_mode: BlendMode = BlendMode.NORMAL
    mask: MaskObject | None = None

    def __post_init__(self) -> None:
        _layer_common(
            self.layer_id, self.name, self.visible, self.opacity_u16, self.blend_mode
        )
        if not isinstance(self.adjustment, Adjustment):
            raise DomainValidationError("adjustment layer requires a typed adjustment")
        if self.mask is not None and not isinstance(self.mask, MaskObject):
            raise DomainValidationError("adjustment layer mask must be typed")


@dataclass(frozen=True, slots=True)
class FontAxis:
    tag: str
    value: float

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or _FONT_AXIS_RE.fullmatch(self.tag) is None:
            raise DomainValidationError("font axis tag must be 4 printable ASCII bytes")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise DomainValidationError("font axis value must be numeric")
        parsed = float(self.value)
        if not math.isfinite(parsed):
            raise DomainValidationError("font axis value must be finite")
        object.__setattr__(self, "value", 0.0 if parsed == 0.0 else parsed)

    @classmethod
    def from_data(cls, value: object) -> FontAxis:
        if not isinstance(value, dict) or set(value) != {"tag", "value"}:
            raise DomainValidationError("font axis has missing or unknown fields")
        return cls(tag=value["tag"], value=value["value"])

    def to_data(self) -> dict[str, object]:
        return {"tag": self.tag, "value": self.value}


@dataclass(frozen=True, slots=True)
class FontFallback:
    requested_family: str
    resolved_family: str
    resolved_font_digest: ObjectId | None
    reason: str

    def __post_init__(self) -> None:
        _text(self.requested_family, "requested font family", maximum=256)
        _text(self.resolved_family, "resolved font family", maximum=256)
        _text(self.reason, "font fallback reason", maximum=512)
        if self.resolved_font_digest is not None and not isinstance(
            self.resolved_font_digest, ObjectId
        ):
            raise DomainValidationError("resolved font must be content-addressed")

    @classmethod
    def from_data(cls, value: object) -> FontFallback:
        required = {"requestedFamily", "resolvedFamily", "resolvedFontSha256", "reason"}
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError("font fallback has missing or unknown fields")
        return cls(
            requested_family=value["requestedFamily"],
            resolved_family=value["resolvedFamily"],
            resolved_font_digest=(
                None
                if value["resolvedFontSha256"] is None
                else ObjectId.parse(value["resolvedFontSha256"])
            ),
            reason=value["reason"],
        )

    def to_data(self) -> dict[str, object]:
        return {
            "requestedFamily": self.requested_family,
            "resolvedFamily": self.resolved_family,
            "resolvedFontSha256": (
                None
                if self.resolved_font_digest is None
                else self.resolved_font_digest.value
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TextLayout:
    width: int
    height: int
    alignment: TextAlignment
    language: str

    def __post_init__(self) -> None:
        _positive(self.width, "text layout width")
        _positive(self.height, "text layout height")
        if not isinstance(self.alignment, TextAlignment):
            raise DomainValidationError("text alignment must be closed")
        _text(self.language, "text language", maximum=64)

    @classmethod
    def from_data(cls, value: object) -> TextLayout:
        if not isinstance(value, dict) or set(value) != {
            "width",
            "height",
            "alignment",
            "language",
        }:
            raise DomainValidationError("text layout has missing or unknown fields")
        return cls(
            width=value["width"],
            height=value["height"],
            alignment=_enum(TextAlignment, value["alignment"], "text alignment"),
            language=value["language"],
        )

    def to_data(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "alignment": self.alignment.value,
            "language": self.language,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TextLayer:
    layer_id: LayerId
    name: str
    text: str
    layout: TextLayout
    font_digest: ObjectId
    face_index: int
    axes: tuple[FontAxis, ...]
    fallbacks: tuple[FontFallback, ...]
    preview_asset_digest: ObjectId
    visible: bool = True
    opacity_u16: int = 65535
    blend_mode: BlendMode = BlendMode.NORMAL
    transform: AffineTransform = AffineTransform()
    mask: MaskObject | None = None

    def __post_init__(self) -> None:
        _layer_common(
            self.layer_id, self.name, self.visible, self.opacity_u16, self.blend_mode
        )
        _text(
            self.text,
            "text layer content",
            maximum=1_048_576,
            allow_multiline=True,
        )
        if not isinstance(self.layout, TextLayout):
            raise DomainValidationError("text layer requires typed layout")
        if isinstance(self.face_index, bool) or not isinstance(self.face_index, int):
            raise DomainValidationError("font face index must be an integer")
        if self.face_index < 0:
            raise DomainValidationError("font face index must be non-negative")
        if not isinstance(self.axes, tuple) or not isinstance(self.fallbacks, tuple):
            raise DomainValidationError("font axes and fallbacks must be immutable tuples")
        if not isinstance(self.font_digest, ObjectId) or not isinstance(
            self.preview_asset_digest, ObjectId
        ):
            raise DomainValidationError("text font and preview must be content-addressed")
        if not isinstance(self.transform, AffineTransform):
            raise DomainValidationError("text layer transform must be affine")
        if self.mask is not None and not isinstance(self.mask, MaskObject):
            raise DomainValidationError("text layer mask must be typed")
        if any(not isinstance(item, FontAxis) for item in self.axes):
            raise DomainValidationError("text layer contains an untyped font axis")
        if any(not isinstance(item, FontFallback) for item in self.fallbacks):
            raise DomainValidationError("text layer contains an untyped fallback")
        tags = [axis.tag for axis in self.axes]
        if len(tags) != len(set(tags)):
            raise DomainValidationError("font axes contain duplicate tags")
        normalized_axes = tuple(sorted(self.axes, key=lambda item: item.tag))
        if normalized_axes != self.axes:
            object.__setattr__(self, "axes", normalized_axes)


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupLayer:
    layer_id: LayerId
    name: str
    child_layer_ids: tuple[LayerId, ...]
    visible: bool = True
    opacity_u16: int = 65535
    blend_mode: BlendMode = BlendMode.NORMAL
    transform: AffineTransform = AffineTransform()
    mask: MaskObject | None = None

    def __post_init__(self) -> None:
        _layer_common(
            self.layer_id, self.name, self.visible, self.opacity_u16, self.blend_mode
        )
        if not isinstance(self.child_layer_ids, tuple):
            raise DomainValidationError("group children must be an immutable tuple")
        if any(not isinstance(item, LayerId) for item in self.child_layer_ids):
            raise DomainValidationError("group contains an untyped child ID")
        if not isinstance(self.transform, AffineTransform):
            raise DomainValidationError("group transform must be affine")
        if self.mask is not None and not isinstance(self.mask, MaskObject):
            raise DomainValidationError("group mask must be typed")
        if len(self.child_layer_ids) != len(set(self.child_layer_ids)):
            raise DomainValidationError("group contains a duplicate child")
        if self.layer_id in self.child_layer_ids:
            raise DomainValidationError("group cannot contain itself")


Layer: TypeAlias = PixelLayer | AdjustmentLayer | TextLayer | GroupLayer


@dataclass(frozen=True, slots=True)
class Selection:
    kind: SelectionKind
    object_id: ObjectId
    bounds: Rect

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SelectionKind):
            raise DomainValidationError("selection kind must be closed")
        if not isinstance(self.object_id, ObjectId) or not isinstance(self.bounds, Rect):
            raise DomainValidationError("selection requires typed object and bounds")

    @classmethod
    def from_data(cls, value: object) -> Selection:
        if not isinstance(value, dict) or set(value) != {"kind", "objectSha256", "bounds"}:
            raise DomainValidationError("selection has missing or unknown fields")
        return cls(
            kind=_enum(SelectionKind, value["kind"], "selection kind"),
            object_id=ObjectId.parse(value["objectSha256"]),
            bounds=Rect.from_data(value["bounds"]),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "objectSha256": self.object_id.value,
            "bounds": self.bounds.to_data(),
        }


def _base_data(layer: Layer) -> dict[str, object]:
    return {
        "id": layer.layer_id.value,
        "name": layer.name,
        "visible": layer.visible,
        "opacityU16": layer.opacity_u16,
        "blendMode": layer.blend_mode.value,
        "mask": None if layer.mask is None else layer.mask.to_data(),
    }


def layer_to_data(layer: Layer) -> dict[str, object]:
    data = _base_data(layer)
    if isinstance(layer, PixelLayer):
        data.update(
            {
                "type": "pixel",
                "assetSha256": layer.asset_digest.value,
                "transform": layer.transform.to_data(),
                "operationProvenance": (
                    None
                    if layer.operation_provenance is None
                    else layer.operation_provenance.to_data()
                ),
            }
        )
    elif isinstance(layer, AdjustmentLayer):
        data.update({"type": "adjustment", "adjustment": layer.adjustment.to_data()})
    elif isinstance(layer, TextLayer):
        data.update(
            {
                "type": "text",
                "text": layer.text,
                "layout": layer.layout.to_data(),
                "fontSha256": layer.font_digest.value,
                "faceIndex": layer.face_index,
                "axes": [axis.to_data() for axis in layer.axes],
                "fallbacks": [fallback.to_data() for fallback in layer.fallbacks],
                "previewAssetSha256": layer.preview_asset_digest.value,
                "transform": layer.transform.to_data(),
            }
        )
    elif isinstance(layer, GroupLayer):
        data.update(
            {
                "type": "group",
                "childLayerIds": [item.value for item in layer.child_layer_ids],
                "transform": layer.transform.to_data(),
            }
        )
    else:  # pragma: no cover - closed union guard
        raise DomainValidationError("unknown layer type")
    return data


def _common_from_data(value: dict[str, object]) -> dict[str, object]:
    return {
        "layer_id": LayerId.parse(value["id"]),
        "name": value["name"],
        "visible": value["visible"],
        "opacity_u16": value["opacityU16"],
        "blend_mode": _enum(BlendMode, value["blendMode"], "blend mode"),
        "mask": None if value["mask"] is None else MaskObject.from_data(value["mask"]),
    }


def layer_from_data(value: object) -> Layer:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise DomainValidationError("layer must be an object with a type")
    common = {"type", "id", "name", "visible", "opacityU16", "blendMode", "mask"}
    layer_type = value["type"]
    if layer_type == "pixel":
        required = common | {"assetSha256", "transform", "operationProvenance"}
        if set(value) != required:
            raise DomainValidationError("pixel layer has missing or unknown fields")
        return PixelLayer(
            **_common_from_data(value),
            asset_digest=ObjectId.parse(value["assetSha256"]),
            transform=AffineTransform.from_data(value["transform"]),
            operation_provenance=(
                None
                if value["operationProvenance"] is None
                else OperationProvenance.from_data(value["operationProvenance"])
            ),
        )
    if layer_type == "adjustment":
        if set(value) != common | {"adjustment"}:
            raise DomainValidationError("adjustment layer has missing or unknown fields")
        return AdjustmentLayer(
            **_common_from_data(value),
            adjustment=Adjustment.from_data(value["adjustment"]),
        )
    if layer_type == "text":
        required = common | {
            "text",
            "layout",
            "fontSha256",
            "faceIndex",
            "axes",
            "fallbacks",
            "previewAssetSha256",
            "transform",
        }
        if set(value) != required:
            raise DomainValidationError("text layer has missing or unknown fields")
        axes = value["axes"]
        fallbacks = value["fallbacks"]
        if not isinstance(axes, list) or not isinstance(fallbacks, list):
            raise DomainValidationError("text axes and fallbacks must be lists")
        return TextLayer(
            **_common_from_data(value),
            text=value["text"],
            layout=TextLayout.from_data(value["layout"]),
            font_digest=ObjectId.parse(value["fontSha256"]),
            face_index=value["faceIndex"],
            axes=tuple(FontAxis.from_data(item) for item in axes),
            fallbacks=tuple(FontFallback.from_data(item) for item in fallbacks),
            preview_asset_digest=ObjectId.parse(value["previewAssetSha256"]),
            transform=AffineTransform.from_data(value["transform"]),
        )
    if layer_type == "group":
        if set(value) != common | {"childLayerIds", "transform"}:
            raise DomainValidationError("group layer has missing or unknown fields")
        children = value["childLayerIds"]
        if not isinstance(children, list):
            raise DomainValidationError("group children must be a list")
        return GroupLayer(
            **_common_from_data(value),
            child_layer_ids=tuple(LayerId.parse(item) for item in children),
            transform=AffineTransform.from_data(value["transform"]),
        )
    raise DomainValidationError(f"unsupported layer type: {layer_type!r}")


def referenced_asset_digests(layer: Layer) -> tuple[ObjectId, ...]:
    if isinstance(layer, PixelLayer):
        return (layer.asset_digest,)
    if isinstance(layer, TextLayer):
        return (layer.preview_asset_digest,)
    return ()


def referenced_object_digests(layer: Layer) -> tuple[ObjectId, ...]:
    values: list[ObjectId] = list(referenced_asset_digests(layer))
    if layer.mask is not None:
        values.append(layer.mask.object_id)
        if layer.mask.source_ref is not None:
            values.append(layer.mask.source_ref)
    return tuple(values)
