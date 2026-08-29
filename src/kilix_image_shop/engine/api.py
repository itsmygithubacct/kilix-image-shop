"""Native-library-free values and protocols for the image engine boundary.

No object in this module can carry a native GEGL or babl handle.  The graph
language is deliberately closed: its nine semantic node families are selected
with enums and typed parameter records, never operation or property strings.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol, TypeAlias, runtime_checkable

from kilix_image_shop.domain.color import (
    AlphaAssociation,
    ConversionPolicy,
)
from kilix_image_shop.domain.geometry import AffineTransform, Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.domain.layers import Adjustment, BlendMode


class EngineFailureCode(StrEnum):
    """The eight stable failure families exposed outside an engine adapter."""

    UNAVAILABLE_GROUP = "unavailable-group"
    INCOMPATIBLE_RUNTIME = "incompatible-runtime"
    UNSUPPORTED_OPERATION = "unsupported-operation"
    INVALID_GRAPH = "invalid-graph"
    DECODE_REFUSAL = "decode-refusal"
    RESOURCE_EXHAUSTION = "resource-exhaustion"
    CANCELLED_OR_STALE_WORK = "cancelled-or-stale-work"
    INTERNAL_ENGINE_FAILURE = "internal-engine-failure"


_DIAGNOSTIC_REF_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


class EngineFailure(RuntimeError):
    """Base for local failures that disclose no native exception or user path."""

    code: ClassVar[EngineFailureCode]

    def __init__(self, message: str, *, diagnostic_ref: str | None = None) -> None:
        if not isinstance(message, str) or not message or len(message) > 256:
            raise ValueError("engine failure message must be a bounded string")
        if diagnostic_ref is not None and (
            not isinstance(diagnostic_ref, str)
            or _DIAGNOSTIC_REF_RE.fullmatch(diagnostic_ref) is None
        ):
            raise ValueError("diagnostic reference must be opaque and canonical")
        super().__init__(message)
        self.diagnostic_ref = diagnostic_ref


class UnavailableGroup(EngineFailure):
    code = EngineFailureCode.UNAVAILABLE_GROUP


class IncompatibleRuntime(EngineFailure):
    code = EngineFailureCode.INCOMPATIBLE_RUNTIME


class UnsupportedOperation(EngineFailure):
    code = EngineFailureCode.UNSUPPORTED_OPERATION


class InvalidGraph(EngineFailure):
    code = EngineFailureCode.INVALID_GRAPH


class DecodeRefusal(EngineFailure):
    code = EngineFailureCode.DECODE_REFUSAL


class ResourceExhaustion(EngineFailure):
    code = EngineFailureCode.RESOURCE_EXHAUSTION


class CancelledOrStaleWork(EngineFailure):
    code = EngineFailureCode.CANCELLED_OR_STALE_WORK


class InternalEngineFailure(EngineFailure):
    code = EngineFailureCode.INTERNAL_ENGINE_FAILURE


ENGINE_FAILURE_TYPES: tuple[type[EngineFailure], ...] = (
    UnavailableGroup,
    IncompatibleRuntime,
    UnsupportedOperation,
    InvalidGraph,
    DecodeRefusal,
    ResourceExhaustion,
    CancelledOrStaleWork,
    InternalEngineFailure,
)


def _require_type(value: object, expected: type[object], field_name: str) -> None:
    if not isinstance(value, expected):
        raise InvalidGraph(f"{field_name} has the wrong type")


def _non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidGraph(f"{field_name} must be a non-negative integer")
    return value


def _opaque_token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise InvalidGraph(f"{field_name} must be a bounded opaque string")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise InvalidGraph(f"{field_name} contains a forbidden character")
    return value


class PixelFormat(StrEnum):
    RGBA_U16 = "RGBA u16"
    RGBA_FLOAT = "RGBA float"
    Y_U8 = "Y u8"

    @property
    def bytes_per_pixel(self) -> int:
        return {
            PixelFormat.RGBA_U16: 8,
            PixelFormat.RGBA_FLOAT: 16,
            PixelFormat.Y_U8: 1,
        }[self]


class MaskSemantics(StrEnum):
    NONE = "none"
    FOREGROUND_ALPHA = "foreground-alpha"


MASK_TILE_SIZE = 256


@dataclass(frozen=True, slots=True)
class PixelSpec:
    """An explicit pixel boundary: format, alpha, profile and mask meaning."""

    pixel_format: PixelFormat
    alpha_association: AlphaAssociation
    profile_digest: ObjectId | None
    mask_semantics: MaskSemantics = MaskSemantics.NONE

    def __post_init__(self) -> None:
        _require_type(self.pixel_format, PixelFormat, "pixel format")
        _require_type(self.alpha_association, AlphaAssociation, "alpha association")
        _require_type(self.mask_semantics, MaskSemantics, "mask semantics")
        if self.pixel_format is PixelFormat.Y_U8:
            if self.alpha_association is not AlphaAssociation.OPAQUE:
                raise InvalidGraph("Y u8 masks must use opaque sample association")
            if self.profile_digest is not None:
                raise InvalidGraph("Y u8 masks cannot carry an ICC profile")
            if self.mask_semantics is not MaskSemantics.FOREGROUND_ALPHA:
                raise InvalidGraph("Y u8 masks must mean foreground alpha")
            return
        if not isinstance(self.profile_digest, ObjectId):
            raise InvalidGraph("RGBA pixels require a content-addressed ICC profile")
        if self.mask_semantics is not MaskSemantics.NONE:
            raise InvalidGraph("RGBA pixels cannot carry mask semantics")

    @classmethod
    def colour(
        cls,
        pixel_format: PixelFormat,
        profile_digest: ObjectId,
        *,
        alpha_association: AlphaAssociation = AlphaAssociation.STRAIGHT,
    ) -> PixelSpec:
        return cls(pixel_format, alpha_association, profile_digest)

    @classmethod
    def foreground_mask(cls) -> PixelSpec:
        return cls(
            PixelFormat.Y_U8,
            AlphaAssociation.OPAQUE,
            None,
            MaskSemantics.FOREGROUND_ALPHA,
        )

    def to_data(self) -> dict[str, str | None]:
        return {
            "format": self.pixel_format.value,
            "alphaAssociation": self.alpha_association.value,
            "profileSha256": (
                None if self.profile_digest is None else self.profile_digest.value
            ),
            "maskSemantics": self.mask_semantics.value,
        }


@dataclass(frozen=True, slots=True)
class BufferRef:
    """An opaque runtime token; no native buffer object can cross this boundary."""

    token: str
    extent: Rect
    spec: PixelSpec
    revision: RevisionId
    content_digest: ObjectId

    def __post_init__(self) -> None:
        _opaque_token(self.token, "buffer token")
        _require_type(self.extent, Rect, "buffer extent")
        _require_type(self.spec, PixelSpec, "buffer pixel spec")
        _require_type(self.revision, RevisionId, "buffer revision")
        _require_type(self.content_digest, ObjectId, "buffer content digest")

    def to_data(self) -> dict[str, object]:
        return {
            "token": self.token,
            "extent": self.extent.to_data(),
            "spec": self.spec.to_data(),
            "revision": self.revision.value,
            "contentSha256": self.content_digest.value,
        }


@dataclass(frozen=True, slots=True)
class MaskTileDigest:
    rectangle: Rect
    digest: ObjectId

    def __post_init__(self) -> None:
        _require_type(self.rectangle, Rect, "mask tile rectangle")
        _require_type(self.digest, ObjectId, "mask tile digest")

    def to_data(self) -> dict[str, object]:
        return {
            "rectangle": self.rectangle.to_data(),
            "sha256": self.digest.value,
        }


@dataclass(frozen=True, slots=True)
class MaskTileUpdate:
    rectangle: Rect
    before_digest: ObjectId
    payload: bytes

    def __post_init__(self) -> None:
        _require_type(self.rectangle, Rect, "mask update rectangle")
        _require_type(self.before_digest, ObjectId, "mask update before digest")
        if not isinstance(self.payload, bytes) or len(self.payload) != (
            self.rectangle.width * self.rectangle.height
        ):
            raise InvalidGraph("mask update payload differs from its Y u8 rectangle")

    @property
    def after_digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.payload)


def mask_tile_rectangles(extent: Rect) -> tuple[Rect, ...]:
    _require_type(extent, Rect, "mask extent")
    rectangles: list[Rect] = []
    bottom = extent.y + extent.height
    right = extent.x + extent.width
    for y in range(extent.y, bottom, MASK_TILE_SIZE):
        height = min(MASK_TILE_SIZE, bottom - y)
        for x in range(extent.x, right, MASK_TILE_SIZE):
            width = min(MASK_TILE_SIZE, right - x)
            rectangles.append(Rect(x, y, width, height))
    return tuple(rectangles)


def _extract_rectangle(payload: bytes, extent: Rect, rectangle: Rect) -> bytes:
    if not rectangle.is_within(extent):
        raise InvalidGraph("mask tile leaves its buffer extent")
    rows: list[bytes] = []
    offset_x = rectangle.x - extent.x
    for y in range(rectangle.y - extent.y, rectangle.y - extent.y + rectangle.height):
        start = y * extent.width + offset_x
        rows.append(payload[start : start + rectangle.width])
    return b"".join(rows)


def _replace_rectangle(
    payload: bytearray,
    extent: Rect,
    rectangle: Rect,
    replacement: bytes,
) -> None:
    offset_x = rectangle.x - extent.x
    replacement_offset = 0
    for y in range(rectangle.y - extent.y, rectangle.y - extent.y + rectangle.height):
        start = y * extent.width + offset_x
        end = start + rectangle.width
        payload[start:end] = replacement[
            replacement_offset : replacement_offset + rectangle.width
        ]
        replacement_offset += rectangle.width


def mask_digest_index(payload: bytes, extent: Rect) -> tuple[MaskTileDigest, ...]:
    _require_type(extent, Rect, "mask extent")
    if not isinstance(payload, bytes) or len(payload) != extent.width * extent.height:
        raise InvalidGraph("mask payload differs from its complete Y u8 extent")
    return tuple(
        MaskTileDigest(
            rectangle,
            ObjectId.from_bytes(_extract_rectangle(payload, extent, rectangle)),
        )
        for rectangle in mask_tile_rectangles(extent)
    )


def mask_manifest_digest(
    extent: Rect,
    tiles: tuple[MaskTileDigest, ...],
) -> ObjectId:
    _require_type(extent, Rect, "mask extent")
    if not isinstance(tiles, tuple) or any(
        not isinstance(item, MaskTileDigest) for item in tiles
    ):
        raise InvalidGraph("mask digest index must be an immutable typed tuple")
    expected = mask_tile_rectangles(extent)
    if tuple(item.rectangle for item in tiles) != expected:
        raise InvalidGraph("mask digest index does not cover the exact sparse tile grid")
    value = {
        "schema": "kilix.imageshop.mask-tile-tree/v1",
        "extent": extent.to_data(),
        "tileSize": MASK_TILE_SIZE,
        "tiles": [item.to_data() for item in tiles],
    }
    carrier = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return ObjectId.from_bytes(carrier)


class GraphNodeKind(StrEnum):
    PIXEL_SOURCE = "pixel-source"
    TEXT_SOURCE = "text-source"
    AFFINE_TRANSFORM_CROP = "affine-transform-crop"
    OPACITY_BLEND = "opacity-blend"
    MASK = "mask"
    ADJUSTMENT = "adjustment"
    ORDERED_GROUP = "ordered-group"
    COLOUR_CONVERSION = "colour-conversion"
    DESTINATION_CROP_SCALE = "destination-crop-scale"


@dataclass(frozen=True, slots=True)
class PixelSourceParameters:
    object_digest: ObjectId
    extent: Rect

    def __post_init__(self) -> None:
        _require_type(self.object_digest, ObjectId, "pixel-source object")
        _require_type(self.extent, Rect, "pixel-source extent")

    def to_data(self) -> dict[str, object]:
        return {
            "objectSha256": self.object_digest.value,
            "extent": self.extent.to_data(),
        }


@dataclass(frozen=True, slots=True)
class TextSourceParameters:
    text_digest: ObjectId
    font_digest: ObjectId
    render_identity: ObjectId
    extent: Rect

    def __post_init__(self) -> None:
        _require_type(self.text_digest, ObjectId, "text object")
        _require_type(self.font_digest, ObjectId, "font object")
        _require_type(self.render_identity, ObjectId, "text render identity")
        _require_type(self.extent, Rect, "text raster extent")

    def to_data(self) -> dict[str, object]:
        return {
            "textSha256": self.text_digest.value,
            "fontSha256": self.font_digest.value,
            "renderIdentitySha256": self.render_identity.value,
            "extent": self.extent.to_data(),
        }


@dataclass(frozen=True, slots=True)
class AffineTransformCropParameters:
    transform: AffineTransform
    crop: Rect

    def __post_init__(self) -> None:
        _require_type(self.transform, AffineTransform, "affine transform")
        _require_type(self.crop, Rect, "affine crop")

    def to_data(self) -> dict[str, object]:
        return {"transform": self.transform.to_data(), "crop": self.crop.to_data()}


@dataclass(frozen=True, slots=True)
class OpacityBlendParameters:
    opacity_u16: int
    blend_mode: BlendMode

    def __post_init__(self) -> None:
        if (
            isinstance(self.opacity_u16, bool)
            or not isinstance(self.opacity_u16, int)
            or not 0 <= self.opacity_u16 <= 65535
        ):
            raise InvalidGraph("opacity must be an integer in [0, 65535]")
        _require_type(self.blend_mode, BlendMode, "blend mode")

    def to_data(self) -> dict[str, object]:
        return {"opacityU16": self.opacity_u16, "blendMode": self.blend_mode.value}


@dataclass(frozen=True, slots=True)
class MaskParameters:
    inverted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.inverted, bool):
            raise InvalidGraph("mask inversion must be boolean")

    def to_data(self) -> dict[str, bool]:
        return {"inverted": self.inverted}


@dataclass(frozen=True, slots=True)
class AdjustmentParameters:
    adjustment: Adjustment

    def __post_init__(self) -> None:
        _require_type(self.adjustment, Adjustment, "adjustment")

    def to_data(self) -> dict[str, object]:
        return self.adjustment.to_data()


@dataclass(frozen=True, slots=True)
class OrderedGroupParameters:
    isolated: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.isolated, bool):
            raise InvalidGraph("group isolation must be boolean")

    def to_data(self) -> dict[str, bool]:
        return {"isolated": self.isolated}


@dataclass(frozen=True, slots=True)
class ColourConversionParameters:
    source_profile: ObjectId
    destination_profile: ObjectId
    conversion_policy: ConversionPolicy

    def __post_init__(self) -> None:
        _require_type(self.source_profile, ObjectId, "source profile")
        _require_type(self.destination_profile, ObjectId, "destination profile")
        _require_type(self.conversion_policy, ConversionPolicy, "conversion policy")

    def to_data(self) -> dict[str, str]:
        return {
            "sourceProfileSha256": self.source_profile.value,
            "destinationProfileSha256": self.destination_profile.value,
            "conversionPolicy": self.conversion_policy.value,
        }


@dataclass(frozen=True, slots=True)
class DestinationCropScaleParameters:
    source: Rect
    destination: Rect

    def __post_init__(self) -> None:
        _require_type(self.source, Rect, "destination source rectangle")
        _require_type(self.destination, Rect, "destination rectangle")

    def to_data(self) -> dict[str, object]:
        return {"source": self.source.to_data(), "destination": self.destination.to_data()}


GraphParameters: TypeAlias = (
    PixelSourceParameters
    | TextSourceParameters
    | AffineTransformCropParameters
    | OpacityBlendParameters
    | MaskParameters
    | AdjustmentParameters
    | OrderedGroupParameters
    | ColourConversionParameters
    | DestinationCropScaleParameters
)


_PARAMETER_TYPE: dict[GraphNodeKind, type[object]] = {
    GraphNodeKind.PIXEL_SOURCE: PixelSourceParameters,
    GraphNodeKind.TEXT_SOURCE: TextSourceParameters,
    GraphNodeKind.AFFINE_TRANSFORM_CROP: AffineTransformCropParameters,
    GraphNodeKind.OPACITY_BLEND: OpacityBlendParameters,
    GraphNodeKind.MASK: MaskParameters,
    GraphNodeKind.ADJUSTMENT: AdjustmentParameters,
    GraphNodeKind.ORDERED_GROUP: OrderedGroupParameters,
    GraphNodeKind.COLOUR_CONVERSION: ColourConversionParameters,
    GraphNodeKind.DESTINATION_CROP_SCALE: DestinationCropScaleParameters,
}

_INPUT_ARITY: dict[GraphNodeKind, tuple[int, int | None]] = {
    GraphNodeKind.PIXEL_SOURCE: (0, 0),
    GraphNodeKind.TEXT_SOURCE: (0, 0),
    GraphNodeKind.AFFINE_TRANSFORM_CROP: (1, 1),
    # One input applies opacity to a bottom layer before transparent
    # composition; two inputs apply opacity and blend over a backdrop.
    GraphNodeKind.OPACITY_BLEND: (1, 2),
    GraphNodeKind.MASK: (2, 2),
    GraphNodeKind.ADJUSTMENT: (1, 1),
    GraphNodeKind.ORDERED_GROUP: (1, None),
    GraphNodeKind.COLOUR_CONVERSION: (1, 1),
    GraphNodeKind.DESTINATION_CROP_SCALE: (1, 1),
}

_NODE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")


@dataclass(frozen=True, slots=True)
class GraphNodeSpec:
    node_id: str
    kind: GraphNodeKind
    inputs: tuple[str, ...]
    parameters: GraphParameters
    output_spec: PixelSpec
    halo_pixels: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.node_id, str)
            or len(self.node_id) > 128
            or _NODE_ID_RE.fullmatch(self.node_id) is None
        ):
            raise InvalidGraph("graph node ID is not canonical")
        _require_type(self.kind, GraphNodeKind, "graph node kind")
        if not isinstance(self.inputs, tuple) or any(
            not isinstance(item, str) or _NODE_ID_RE.fullmatch(item) is None
            for item in self.inputs
        ):
            raise InvalidGraph("graph inputs must be canonical immutable node IDs")
        if len(set(self.inputs)) != len(self.inputs):
            raise InvalidGraph("graph node cannot repeat an input")
        expected_parameters = _PARAMETER_TYPE[self.kind]
        if not isinstance(self.parameters, expected_parameters):
            raise InvalidGraph("graph node parameters do not match its closed family")
        _require_type(self.output_spec, PixelSpec, "graph output pixel spec")
        _non_negative_integer(self.halo_pixels, "graph halo")
        minimum, maximum = _INPUT_ARITY[self.kind]
        if len(self.inputs) < minimum or (
            maximum is not None and len(self.inputs) > maximum
        ):
            raise InvalidGraph("graph node has the wrong input arity")

    def to_data(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "kind": self.kind.value,
            "inputs": list(self.inputs),
            "parameters": self.parameters.to_data(),
            "outputSpec": self.output_spec.to_data(),
            "haloPixels": self.halo_pixels,
        }


def _require_matching_specs(node: GraphNodeSpec, by_id: dict[str, GraphNodeSpec]) -> None:
    inputs = tuple(by_id[item] for item in node.inputs)
    if node.kind is GraphNodeKind.TEXT_SOURCE:
        if node.output_spec.pixel_format is PixelFormat.Y_U8:
            raise InvalidGraph("text source must produce profiled RGBA pixels")
        return
    if node.kind is GraphNodeKind.MASK:
        if inputs[1].output_spec != PixelSpec.foreground_mask():
            raise InvalidGraph("mask input must be Y u8 foreground alpha")
        if inputs[0].output_spec != node.output_spec:
            raise InvalidGraph("mask application must preserve colour pixel spec")
        return
    if node.kind is GraphNodeKind.OPACITY_BLEND:
        if any(item.output_spec != node.output_spec for item in inputs):
            raise InvalidGraph("blend inputs and output must share one pixel spec")
        return
    if node.kind is GraphNodeKind.ORDERED_GROUP:
        if any(item.output_spec != node.output_spec for item in inputs):
            raise InvalidGraph("ordered group inputs must share its output pixel spec")
        return
    if node.kind is GraphNodeKind.COLOUR_CONVERSION:
        parameters = node.parameters
        assert isinstance(parameters, ColourConversionParameters)
        source = inputs[0].output_spec
        if source.profile_digest != parameters.source_profile:
            raise InvalidGraph("colour conversion source profile does not match input")
        if node.output_spec.profile_digest != parameters.destination_profile:
            raise InvalidGraph("colour conversion destination profile does not match output")
        if source.pixel_format is not node.output_spec.pixel_format:
            raise InvalidGraph("colour conversion cannot change the working format")
        if source.alpha_association is not node.output_spec.alpha_association:
            raise InvalidGraph("colour conversion cannot change alpha association")
        return
    if inputs and inputs[0].output_spec != node.output_spec:
        raise InvalidGraph("graph family must preserve its input pixel spec")


@dataclass(frozen=True, slots=True)
class GraphSpec:
    """A closed, topologically ordered DAG for exactly one document revision."""

    revision: RevisionId
    compatibility_digest: ObjectId
    nodes: tuple[GraphNodeSpec, ...]
    output_node: str
    schema: str = "kilix.imageshop.graph/v1"

    SCHEMA: ClassVar[str] = "kilix.imageshop.graph/v1"

    def __post_init__(self) -> None:
        if self.schema != self.SCHEMA:
            raise InvalidGraph("unsupported graph schema")
        _require_type(self.revision, RevisionId, "graph revision")
        _require_type(self.compatibility_digest, ObjectId, "compatibility digest")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise InvalidGraph("graph must contain an immutable non-empty node tuple")
        if any(not isinstance(node, GraphNodeSpec) for node in self.nodes):
            raise InvalidGraph("graph contains an untyped node")

        by_id: dict[str, GraphNodeSpec] = {}
        for node in self.nodes:
            if node.node_id in by_id:
                raise InvalidGraph("graph node IDs must be unique")
            if any(input_id not in by_id for input_id in node.inputs):
                raise InvalidGraph("graph must be topologically ordered")
            by_id[node.node_id] = node
            _require_matching_specs(node, by_id)

        if self.output_node not in by_id:
            raise InvalidGraph("graph output node is missing")
        output = by_id[self.output_node]
        if output is not self.nodes[-1]:
            raise InvalidGraph("graph output must be the final topological node")
        if output.kind is not GraphNodeKind.DESTINATION_CROP_SCALE:
            raise InvalidGraph("graph must end in an exact destination node")

        reachable: set[str] = set()
        pending = [self.output_node]
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            pending.extend(by_id[node_id].inputs)
        if reachable != set(by_id):
            raise InvalidGraph("graph contains a node that cannot reach its output")

    @property
    def output_spec(self) -> PixelSpec:
        return self.nodes[-1].output_spec

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "revision": self.revision.value,
            "compatibilitySha256": self.compatibility_digest.value,
            "nodes": [node.to_data() for node in self.nodes],
            "outputNode": self.output_node,
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_data(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> ObjectId:
        return ObjectId.from_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class TileRequest:
    graph_digest: ObjectId
    source: Rect
    destination: Rect
    level: int
    spec: PixelSpec
    revision: RevisionId

    MAX_WIDTH: ClassVar[int] = 1920
    MAX_HEIGHT: ClassVar[int] = 1080

    def __post_init__(self) -> None:
        _require_type(self.graph_digest, ObjectId, "tile graph digest")
        _require_type(self.source, Rect, "tile source rectangle")
        _require_type(self.destination, Rect, "tile destination rectangle")
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 3
        ):
            raise InvalidGraph("tile level must be in [0, 3]")
        _require_type(self.spec, PixelSpec, "tile pixel spec")
        _require_type(self.revision, RevisionId, "tile revision")
        if (
            self.destination.width > self.MAX_WIDTH
            or self.destination.height > self.MAX_HEIGHT
        ):
            raise ResourceExhaustion("destination tile exceeds the bounded tile envelope")

    def to_data(self) -> dict[str, object]:
        return {
            "graphSha256": self.graph_digest.value,
            "source": self.source.to_data(),
            "destination": self.destination.to_data(),
            "level": self.level,
            "spec": self.spec.to_data(),
            "revision": self.revision.value,
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.to_data(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TileResult:
    source: Rect
    destination: Rect
    level: int
    spec: PixelSpec
    revision: RevisionId
    payload_digest: ObjectId
    elapsed_ns: int
    owned_bytes: bytes | None = None
    buffer_ref: BufferRef | None = None

    def __post_init__(self) -> None:
        _require_type(self.source, Rect, "tile-result source rectangle")
        _require_type(self.destination, Rect, "tile-result destination rectangle")
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 3
        ):
            raise InternalEngineFailure("engine returned an invalid tile level")
        _require_type(self.spec, PixelSpec, "tile-result pixel spec")
        _require_type(self.revision, RevisionId, "tile-result revision")
        _require_type(self.payload_digest, ObjectId, "tile-result payload digest")
        _non_negative_integer(self.elapsed_ns, "tile elapsed time")
        if (self.owned_bytes is None) == (self.buffer_ref is None):
            raise InternalEngineFailure("tile result must own bytes or one buffer reference")
        if self.owned_bytes is not None:
            if not isinstance(self.owned_bytes, bytes):
                raise InternalEngineFailure("tile result bytes must be immutable")
            expected = (
                self.destination.width
                * self.destination.height
                * self.spec.pixel_format.bytes_per_pixel
            )
            if len(self.owned_bytes) != expected:
                raise InternalEngineFailure("tile byte length does not match its geometry")
            if ObjectId.from_bytes(self.owned_bytes) != self.payload_digest:
                raise InternalEngineFailure("tile byte digest does not match its payload")
        else:
            assert self.buffer_ref is not None
            if self.buffer_ref.content_digest != self.payload_digest:
                raise InternalEngineFailure("tile buffer digest does not match its payload")


@dataclass(slots=True)
class CancelToken:
    """A thread-safe monotonic cancellation bit."""

    _event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledOrStaleWork("engine work was cancelled")


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    engine_id: str
    compatibility_digest: ObjectId
    supported_formats: tuple[PixelFormat, ...]
    supported_nodes: tuple[GraphNodeKind, ...]
    proxy_levels: tuple[int, ...]
    max_tile_width: int
    max_tile_height: int

    def __post_init__(self) -> None:
        _opaque_token(self.engine_id, "engine ID")
        _require_type(self.compatibility_digest, ObjectId, "compatibility digest")
        if (
            not isinstance(self.supported_formats, tuple)
            or not self.supported_formats
            or any(not isinstance(item, PixelFormat) for item in self.supported_formats)
            or len(set(self.supported_formats)) != len(self.supported_formats)
        ):
            raise IncompatibleRuntime("engine format capabilities are invalid")
        if (
            not isinstance(self.supported_nodes, tuple)
            or set(self.supported_nodes) != set(GraphNodeKind)
            or len(self.supported_nodes) != len(GraphNodeKind)
        ):
            raise IncompatibleRuntime("engine must declare all closed graph families")
        if self.proxy_levels != (1, 2, 3):
            raise IncompatibleRuntime("engine must declare all three proxy levels")
        if self.max_tile_width != 1920 or self.max_tile_height != 1080:
            raise IncompatibleRuntime("engine tile envelope is incompatible")


@dataclass(frozen=True, slots=True)
class ProcessMemoryDiagnostics:
    resident_bytes: int
    peak_bytes: int

    def __post_init__(self) -> None:
        for value in (self.resident_bytes, self.peak_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("process memory diagnostic is malformed")
        if self.peak_bytes < self.resident_bytes:
            raise InternalEngineFailure("process peak memory is below resident memory")


@dataclass(frozen=True, slots=True)
class SwapDiagnostics:
    bytes_used: int
    file_count: int
    quota_bytes: int | None

    def __post_init__(self) -> None:
        for value in (self.bytes_used, self.file_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("swap diagnostic is malformed")
        if self.quota_bytes is not None and (
            isinstance(self.quota_bytes, bool)
            or not isinstance(self.quota_bytes, int)
            or self.quota_bytes <= 0
        ):
            raise InternalEngineFailure("swap quota diagnostic is malformed")


@dataclass(frozen=True, slots=True)
class BufferInventoryEntry:
    extent: Rect
    spec: PixelSpec
    revision: RevisionId

    def __post_init__(self) -> None:
        _require_type(self.extent, Rect, "buffer diagnostic extent")
        _require_type(self.spec, PixelSpec, "buffer diagnostic pixel spec")
        _require_type(self.revision, RevisionId, "buffer diagnostic revision")


@dataclass(frozen=True, slots=True)
class BufferInventory:
    entries: tuple[BufferInventoryEntry, ...]
    total_pixels: int

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(item, BufferInventoryEntry) for item in self.entries
        ):
            raise InternalEngineFailure("buffer inventory is malformed")
        expected = sum(item.extent.width * item.extent.height for item in self.entries)
        if self.total_pixels != expected:
            raise InternalEngineFailure("buffer inventory pixel count differs")

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class ProxyDiagnostics:
    levels: tuple[int, ...]
    bytes_used: int
    complete_count: int

    def __post_init__(self) -> None:
        if self.levels != tuple(sorted(self.levels)) or any(
            item not in {1, 2, 3} for item in self.levels
        ):
            raise InternalEngineFailure("proxy level diagnostic is malformed")
        for value in (self.bytes_used, self.complete_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("proxy diagnostic count is malformed")
        if self.complete_count != len(self.levels):
            raise InternalEngineFailure("proxy diagnostic completion count differs")


@dataclass(frozen=True, slots=True)
class QueueDiagnostics:
    queued_tiles: int
    running_tiles: int

    def __post_init__(self) -> None:
        for value in (self.queued_tiles, self.running_tiles):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("tile queue diagnostic is malformed")


@dataclass(frozen=True, slots=True)
class TimingDiagnostics:
    last_tile_ns: int | None
    rolling_mean_ns: int | None
    sample_count: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or not 0 <= self.sample_count <= 32
        ):
            raise InternalEngineFailure("tile timing sample count is malformed")
        if self.sample_count == 0:
            if self.last_tile_ns is not None or self.rolling_mean_ns is not None:
                raise InternalEngineFailure("empty tile timing window has values")
            return
        for value in (self.last_tile_ns, self.rolling_mean_ns):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("tile timing diagnostic is malformed")


@dataclass(frozen=True, slots=True)
class EngineDiagnostics:
    """The exact 9/9 safe resource groups exposed by an engine adapter."""

    process_memory: ProcessMemoryDiagnostics
    tile_cache_ceiling_bytes: int
    swap: SwapDiagnostics
    buffers: BufferInventory
    proxies: ProxyDiagnostics
    queue: QueueDiagnostics
    graph_cache_count: int
    export_staging_bytes: int
    timing: TimingDiagnostics

    def __post_init__(self) -> None:
        expected_types = (
            (self.process_memory, ProcessMemoryDiagnostics),
            (self.swap, SwapDiagnostics),
            (self.buffers, BufferInventory),
            (self.proxies, ProxyDiagnostics),
            (self.queue, QueueDiagnostics),
            (self.timing, TimingDiagnostics),
        )
        if any(not isinstance(value, expected) for value, expected in expected_types):
            raise InternalEngineFailure("engine diagnostic group is untyped")
        for value in (
            self.tile_cache_ceiling_bytes,
            self.graph_cache_count,
            self.export_staging_bytes,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEngineFailure("engine diagnostic scalar is malformed")


@runtime_checkable
class ImageEngine(Protocol):
    def start(self) -> EngineCapabilities: ...

    def register_profile(
        self,
        payload: bytes,
        digest: ObjectId,
        *,
        cancel: CancelToken,
    ) -> ObjectId: ...

    def import_pixels(
        self,
        payload: bytes,
        *,
        extent: Rect,
        spec: PixelSpec,
        revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef: ...

    def edit_mask(
        self,
        buffer: BufferRef,
        updates: tuple[MaskTileUpdate, ...],
        *,
        new_revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef: ...

    def compile_graph(self, graph: GraphSpec, *, cancel: CancelToken) -> ObjectId: ...

    def render_tile(self, request: TileRequest, *, cancel: CancelToken) -> TileResult: ...

    def build_proxy(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]: ...

    def invalidate_proxies(self, graph_digests: tuple[ObjectId, ...]) -> int: ...

    def diagnostics(self) -> EngineDiagnostics: ...

    def export_tiles(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]: ...

    def close(self) -> None: ...


class FakeImageEngine:
    """Deterministic in-memory conformance double with owner-thread checks."""

    def __init__(self, *, compatibility_digest: ObjectId | None = None) -> None:
        if compatibility_digest is None:
            compatibility_digest = ObjectId.from_bytes(
                b"kilix-image-shop/fake-engine/v1\n"
            )
        _require_type(compatibility_digest, ObjectId, "fake compatibility digest")
        self._capabilities = EngineCapabilities(
            engine_id="kilix.fake-engine/v1",
            compatibility_digest=compatibility_digest,
            supported_formats=(PixelFormat.RGBA_U16, PixelFormat.Y_U8),
            supported_nodes=tuple(GraphNodeKind),
            proxy_levels=(1, 2, 3),
            max_tile_width=TileRequest.MAX_WIDTH,
            max_tile_height=TileRequest.MAX_HEIGHT,
        )
        self._owner_thread: int | None = None
        self._started = False
        self._closed = False
        self._buffers: dict[str, BufferRef] = {}
        self._buffer_payloads: dict[str, bytes] = {}
        self._mask_indexes: dict[str, tuple[MaskTileDigest, ...]] = {}
        self._graphs: dict[ObjectId, GraphSpec] = {}
        self._profiles: set[ObjectId] = set()
        self._proxy_results: dict[
            tuple[ObjectId, int], tuple[TileResult, ...]
        ] = {}
        self._tile_timings: list[int] = []

    def _bind_or_check_owner(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise InternalEngineFailure(
                "engine access is restricted to its owner executor",
                diagnostic_ref="fake.owner-thread",
            )

    def _require_started(self) -> None:
        self._bind_or_check_owner()
        if not self._started or self._closed:
            raise IncompatibleRuntime("engine is not in a started state")

    def start(self) -> EngineCapabilities:
        self._bind_or_check_owner()
        if self._closed:
            raise IncompatibleRuntime("a closed engine cannot be restarted")
        self._started = True
        return self._capabilities

    def _validate_spec(self, spec: PixelSpec) -> None:
        if spec.pixel_format not in self._capabilities.supported_formats:
            raise UnsupportedOperation("pixel format is unavailable at the active tier")

    def import_pixels(
        self,
        payload: bytes,
        *,
        extent: Rect,
        spec: PixelSpec,
        revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef:
        self._require_started()
        cancel.raise_if_cancelled()
        if not isinstance(payload, bytes):
            raise DecodeRefusal("decoded pixels must be immutable bytes")
        _require_type(extent, Rect, "import extent")
        _require_type(spec, PixelSpec, "import pixel spec")
        _require_type(revision, RevisionId, "import revision")
        self._validate_spec(spec)
        expected = extent.width * extent.height * spec.pixel_format.bytes_per_pixel
        if len(payload) != expected:
            raise DecodeRefusal("decoded byte length does not match extent and format")
        mask_index: tuple[MaskTileDigest, ...] | None = None
        if spec == PixelSpec.foreground_mask():
            mask_index = mask_digest_index(payload, extent)
            digest = mask_manifest_digest(extent, mask_index)
        else:
            digest = ObjectId.from_bytes(payload)
        descriptor = json.dumps(
            {
                "content": digest.value,
                "extent": extent.to_data(),
                "spec": spec.to_data(),
                "revision": revision.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = f"fake:{hashlib.sha256(descriptor).hexdigest()}"
        result = BufferRef(token, extent, spec, revision, digest)
        cancel.raise_if_cancelled()
        self._buffers[token] = result
        self._buffer_payloads[token] = payload
        if mask_index is not None:
            self._mask_indexes[token] = mask_index
        return result

    def edit_mask(
        self,
        buffer: BufferRef,
        updates: tuple[MaskTileUpdate, ...],
        *,
        new_revision: RevisionId,
        cancel: CancelToken,
    ) -> BufferRef:
        self._require_started()
        cancel.raise_if_cancelled()
        if not isinstance(buffer, BufferRef) or self._buffers.get(buffer.token) != buffer:
            raise InvalidGraph("mask edit requires one live opaque buffer reference")
        if buffer.spec != PixelSpec.foreground_mask():
            raise InvalidGraph("mask edit requires a Y u8 foreground-alpha buffer")
        _require_type(new_revision, RevisionId, "mask edit revision")
        if new_revision == buffer.revision:
            raise InvalidGraph("mask edit must advance the buffer revision")
        if not isinstance(updates, tuple) or not updates or any(
            not isinstance(item, MaskTileUpdate) for item in updates
        ):
            raise InvalidGraph("mask edit requires immutable typed tile updates")
        keys = tuple(
            (item.rectangle.y, item.rectangle.x) for item in updates
        )
        if keys != tuple(sorted(set(keys))):
            raise InvalidGraph("mask updates must be sorted and unique")
        current_index = self._mask_indexes[buffer.token]
        current = {item.rectangle: item.digest for item in current_index}
        for update in updates:
            if update.rectangle not in current:
                raise InvalidGraph("mask update is not one exact sparse tile")
            if current[update.rectangle] != update.before_digest:
                raise CancelledOrStaleWork("mask tile before-digest is stale")
            if update.after_digest == update.before_digest:
                raise InvalidGraph("mask update cannot be a no-op")
        revised = dict(current)
        for update in updates:
            revised[update.rectangle] = update.after_digest
        revised_index = tuple(
            MaskTileDigest(item.rectangle, revised[item.rectangle])
            for item in current_index
        )
        content_digest = mask_manifest_digest(buffer.extent, revised_index)
        descriptor = json.dumps(
            {
                "base": buffer.content_digest.value,
                "content": content_digest.value,
                "revision": new_revision.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = f"fake:{hashlib.sha256(descriptor).hexdigest()}"
        existing = self._buffers.get(token)
        if existing is not None:
            return existing
        payload = bytearray(self._buffer_payloads[buffer.token])
        for update in updates:
            cancel.raise_if_cancelled()
            _replace_rectangle(payload, buffer.extent, update.rectangle, update.payload)
        result = BufferRef(
            token,
            buffer.extent,
            buffer.spec,
            new_revision,
            content_digest,
        )
        cancel.raise_if_cancelled()
        self._buffers[token] = result
        self._buffer_payloads[token] = bytes(payload)
        self._mask_indexes[token] = revised_index
        return result

    def register_profile(
        self,
        payload: bytes,
        digest: ObjectId,
        *,
        cancel: CancelToken,
    ) -> ObjectId:
        self._require_started()
        cancel.raise_if_cancelled()
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 4_194_304:
            raise DecodeRefusal("ICC profile must be bounded immutable bytes")
        _require_type(digest, ObjectId, "ICC profile digest")
        if ObjectId.from_bytes(payload) != digest:
            raise DecodeRefusal("ICC profile bytes do not match their content identity")
        self._profiles.add(digest)
        cancel.raise_if_cancelled()
        return digest

    def compile_graph(self, graph: GraphSpec, *, cancel: CancelToken) -> ObjectId:
        self._require_started()
        cancel.raise_if_cancelled()
        _require_type(graph, GraphSpec, "graph")
        if graph.compatibility_digest != self._capabilities.compatibility_digest:
            raise IncompatibleRuntime("graph compatibility identity does not match engine")
        for node in graph.nodes:
            if node.kind not in self._capabilities.supported_nodes:
                raise UnsupportedOperation("graph family is unavailable")
            self._validate_spec(node.output_spec)
            if (
                node.output_spec.profile_digest is not None
                and node.output_spec.profile_digest not in self._profiles
            ):
                raise DecodeRefusal("graph profile has not been registered")
            if isinstance(node.parameters, PixelSourceParameters):
                matching_buffers = tuple(
                    item
                    for item in self._buffers.values()
                    if item.content_digest == node.parameters.object_digest
                    and item.revision == graph.revision
                )
                if not matching_buffers:
                    raise DecodeRefusal(
                        "pixel source has no imported buffer for this revision"
                    )
                if not any(
                    item.extent == node.parameters.extent
                    and item.spec == node.output_spec
                    for item in matching_buffers
                ):
                    raise InvalidGraph(
                        "pixel-source extent or format differs from its imported buffer"
                    )
            if isinstance(node.parameters, TextSourceParameters):
                if not any(
                    item.content_digest == node.parameters.render_identity
                    and item.extent == node.parameters.extent
                    and item.spec == node.output_spec
                    and item.revision == graph.revision
                    for item in self._buffers.values()
                ):
                    raise DecodeRefusal(
                        "text source has no matching raster buffer for this revision"
                    )
        digest = graph.digest
        self._graphs[digest] = graph
        cancel.raise_if_cancelled()
        return digest

    @staticmethod
    def _selected_extent(graph: GraphSpec, level: int) -> Rect:
        parameters = graph.nodes[-1].parameters
        assert isinstance(parameters, DestinationCropScaleParameters)
        extent = parameters.destination
        if level == 0:
            return extent
        denominator = 1 << level
        left = extent.x // denominator
        top = extent.y // denominator
        right = -(-(extent.x + extent.width) // denominator)
        bottom = -(-(extent.y + extent.height) // denominator)
        return Rect(left, top, right - left, bottom - top)

    def _render_tile(
        self,
        request: TileRequest,
        cancel: CancelToken,
        *,
        proxy_build: bool = False,
    ) -> TileResult:
        cancel.raise_if_cancelled()
        graph = self._graphs.get(request.graph_digest)
        if graph is None:
            raise InvalidGraph("tile request names an uncompiled graph")
        if request.revision != graph.revision:
            raise CancelledOrStaleWork("tile request revision is stale")
        if request.spec != graph.output_spec:
            raise InvalidGraph("tile request pixel spec differs from graph output")
        self._validate_spec(request.spec)
        if not proxy_build:
            if request.level > 0 and (
                request.graph_digest,
                request.level,
            ) not in self._proxy_results:
                raise InvalidGraph("tile request names an unavailable proxy level")
            if not request.source.is_within(
                self._selected_extent(graph, request.level)
            ):
                raise InvalidGraph("tile source leaves its selected render level")

        byte_count = (
            request.destination.width
            * request.destination.height
            * request.spec.pixel_format.bytes_per_pixel
        )
        chunks: list[bytes] = []
        bytes_per_pixel = request.spec.pixel_format.bytes_per_pixel
        for y in range(
            request.destination.y,
            request.destination.y + request.destination.height,
        ):
            cancel.raise_if_cancelled()
            for x in range(
                request.destination.x,
                request.destination.x + request.destination.width,
            ):
                identity = (
                    b"kilix-image-shop/fake-pixel/v1\0"
                    + request.graph_digest.value.encode("ascii")
                    + request.revision.value.encode("ascii")
                    + request.level.to_bytes(1, "big")
                    + x.to_bytes(8, "big", signed=True)
                    + y.to_bytes(8, "big", signed=True)
                )
                chunks.append(hashlib.sha256(identity).digest()[:bytes_per_pixel])
        payload = b"".join(chunks)
        if len(payload) != byte_count:
            raise InternalEngineFailure("fake tile generator produced a wrong byte count")
        cancel.raise_if_cancelled()
        return TileResult(
            source=request.source,
            destination=request.destination,
            level=request.level,
            spec=request.spec,
            revision=request.revision,
            payload_digest=ObjectId.from_bytes(payload),
            elapsed_ns=0,
            owned_bytes=payload,
        )

    def render_tile(self, request: TileRequest, *, cancel: CancelToken) -> TileResult:
        self._require_started()
        _require_type(request, TileRequest, "tile request")
        result = self._render_tile(request, cancel)
        self._tile_timings.append(result.elapsed_ns)
        del self._tile_timings[:-32]
        return result

    def build_proxy(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]:
        self._require_started()
        if not isinstance(requests, tuple) or not requests or any(
            not isinstance(item, TileRequest) for item in requests
        ):
            raise InvalidGraph("proxy requests must be a non-empty immutable typed tuple")
        if any(item.level not in self._capabilities.proxy_levels for item in requests):
            raise InvalidGraph("proxy work must target levels 1 through 3")
        identity = (
            requests[0].graph_digest,
            requests[0].level,
            requests[0].spec,
            requests[0].revision,
        )
        if any(
            (
                item.graph_digest,
                item.level,
                item.spec,
                item.revision,
            )
            != identity
            for item in requests
        ):
            raise InvalidGraph("one proxy build must cover exactly one graph level")
        key = (requests[0].graph_digest, requests[0].level)
        existing = self._proxy_results.get(key)
        if existing is not None:
            return existing
        results = tuple(
            self._render_tile(item, cancel, proxy_build=True) for item in requests
        )
        cancel.raise_if_cancelled()
        self._proxy_results[key] = results
        return results

    def invalidate_proxies(self, graph_digests: tuple[ObjectId, ...]) -> int:
        self._require_started()
        if not isinstance(graph_digests, tuple) or any(
            not isinstance(item, ObjectId) for item in graph_digests
        ):
            raise InvalidGraph("proxy invalidation requires typed graph digests")
        identities = tuple(item.value for item in graph_digests)
        if identities != tuple(sorted(set(identities))):
            raise InvalidGraph("proxy invalidation graph digests must be sorted and unique")
        wanted = set(graph_digests)
        removed = tuple(
            key for key in self._proxy_results if key[0] in wanted
        )
        for key in removed:
            del self._proxy_results[key]
        return len(removed)

    def diagnostics(self) -> EngineDiagnostics:
        self._require_started()
        entries = tuple(
            BufferInventoryEntry(item.extent, item.spec, item.revision)
            for _, item in sorted(self._buffers.items())
        )
        proxy_items = tuple(
            sorted(self._proxy_results.items(), key=lambda item: (item[0][0].value, item[0][1]))
        )
        timings = tuple(self._tile_timings)
        return EngineDiagnostics(
            ProcessMemoryDiagnostics(0, 0),
            0,
            SwapDiagnostics(0, 0, None),
            BufferInventory(
                entries,
                sum(item.extent.width * item.extent.height for item in entries),
            ),
            ProxyDiagnostics(
                tuple(sorted(key[1] for key, _ in proxy_items)),
                sum(
                    len(result.owned_bytes or b"")
                    for _, results in proxy_items
                    for result in results
                ),
                len(proxy_items),
            ),
            QueueDiagnostics(0, 0),
            len(self._graphs),
            0,
            TimingDiagnostics(
                None if not timings else timings[-1],
                None if not timings else sum(timings) // len(timings),
                len(timings),
            ),
        )

    def export_tiles(
        self,
        requests: tuple[TileRequest, ...],
        *,
        cancel: CancelToken,
    ) -> tuple[TileResult, ...]:
        self._require_started()
        if not isinstance(requests, tuple) or any(
            not isinstance(item, TileRequest) for item in requests
        ):
            raise InvalidGraph("export requests must be an immutable typed tuple")
        if any(item.level != 0 for item in requests):
            raise InvalidGraph("full-resolution export must use level 0")
        return tuple(self._render_tile(item, cancel) for item in requests)

    def close(self) -> None:
        self._bind_or_check_owner()
        self._buffers.clear()
        self._buffer_payloads.clear()
        self._mask_indexes.clear()
        self._graphs.clear()
        self._profiles.clear()
        self._proxy_results.clear()
        self._tile_timings.clear()
        self._started = False
        self._closed = True


__all__ = (
    "AdjustmentParameters",
    "AffineTransformCropParameters",
    "BufferRef",
    "BufferInventory",
    "BufferInventoryEntry",
    "CancelToken",
    "CancelledOrStaleWork",
    "ColourConversionParameters",
    "DecodeRefusal",
    "DestinationCropScaleParameters",
    "EngineCapabilities",
    "EngineDiagnostics",
    "EngineFailure",
    "EngineFailureCode",
    "FakeImageEngine",
    "GraphNodeKind",
    "GraphNodeSpec",
    "GraphSpec",
    "ImageEngine",
    "IncompatibleRuntime",
    "InternalEngineFailure",
    "InvalidGraph",
    "MaskParameters",
    "MaskSemantics",
    "MASK_TILE_SIZE",
    "MaskTileDigest",
    "MaskTileUpdate",
    "OpacityBlendParameters",
    "OrderedGroupParameters",
    "PixelFormat",
    "PixelSourceParameters",
    "PixelSpec",
    "ProcessMemoryDiagnostics",
    "ProxyDiagnostics",
    "QueueDiagnostics",
    "ResourceExhaustion",
    "TextSourceParameters",
    "SwapDiagnostics",
    "TimingDiagnostics",
    "TileRequest",
    "TileResult",
    "UnavailableGroup",
    "UnsupportedOperation",
    "mask_digest_index",
    "mask_manifest_digest",
    "mask_tile_rectangles",
)
