"""Versioned immutable document state and canonical project serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Self

from .assets import AssetRef
from .color import ColourState, EngineCompatibility
from .geometry import Canvas
from .identifiers import (
    DocumentId,
    DomainValidationError,
    LayerId,
    ObjectId,
    RevisionId,
)
from .layers import (
    AdjustmentLayer,
    GroupLayer,
    Layer,
    OperationProvenance,
    PixelLayer,
    Selection,
    TextLayer,
    layer_from_data,
    layer_to_data,
    referenced_asset_digests,
)


PROJECT_SCHEMA = "kilix.imageshop.project/v1"


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DomainValidationError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise DomainValidationError(f"non-finite JSON number is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class DocumentState:
    schema: str
    document_id: DocumentId
    revision_id: RevisionId
    canvas: Canvas
    colour: ColourState
    engine_compatibility: EngineCompatibility
    assets: tuple[AssetRef, ...]
    root_layer_ids: tuple[LayerId, ...]
    layers: tuple[Layer, ...]
    selection: Selection | None = None
    provenance: tuple[OperationProvenance, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != PROJECT_SCHEMA:
            raise DomainValidationError("unsupported project schema")
        if not isinstance(self.document_id, DocumentId):
            raise DomainValidationError("document ID must be typed")
        if not isinstance(self.revision_id, RevisionId):
            raise DomainValidationError("revision ID must be typed")
        if not isinstance(self.canvas, Canvas):
            raise DomainValidationError("canvas must be typed")
        if not isinstance(self.colour, ColourState):
            raise DomainValidationError("colour state must be typed")
        if not isinstance(self.engine_compatibility, EngineCompatibility):
            raise DomainValidationError("engine compatibility must be typed")
        if self.colour.working_profile != self.engine_compatibility.working_profile:
            raise DomainValidationError("document and engine working profiles disagree")
        if self.colour.conversion_policy != self.engine_compatibility.conversion_policy:
            raise DomainValidationError("document and engine conversion policies disagree")
        if not isinstance(self.assets, tuple):
            raise DomainValidationError("assets must be an immutable tuple")
        if not isinstance(self.layers, tuple):
            raise DomainValidationError("layers must be an immutable tuple")
        if not isinstance(self.root_layer_ids, tuple):
            raise DomainValidationError("root layer IDs must be an immutable tuple")
        if not isinstance(self.provenance, tuple):
            raise DomainValidationError("provenance must be an immutable tuple")

        if any(not isinstance(asset, AssetRef) for asset in self.assets):
            raise DomainValidationError("asset table contains an untyped value")
        normalized_assets = tuple(sorted(self.assets, key=lambda item: item.digest.value))
        if len({asset.digest for asset in normalized_assets}) != len(normalized_assets):
            raise DomainValidationError("asset table contains a duplicate digest")
        object.__setattr__(self, "assets", normalized_assets)

        closed_layer_types = (PixelLayer, AdjustmentLayer, TextLayer, GroupLayer)
        if any(not isinstance(layer, closed_layer_types) for layer in self.layers):
            raise DomainValidationError("layer table contains an untyped value")
        normalized_layers = tuple(sorted(self.layers, key=lambda item: item.layer_id.value))
        layer_ids = [layer.layer_id for layer in normalized_layers]
        if len(set(layer_ids)) != len(layer_ids):
            raise DomainValidationError("layer table contains a duplicate ID")
        object.__setattr__(self, "layers", normalized_layers)

        if any(not isinstance(item, LayerId) for item in self.root_layer_ids):
            raise DomainValidationError("root layer table contains an untyped ID")
        if len(set(self.root_layer_ids)) != len(self.root_layer_ids):
            raise DomainValidationError("root layer table contains a duplicate ID")

        layer_map = {layer.layer_id: layer for layer in normalized_layers}
        root_set = set(self.root_layer_ids)
        if not root_set <= set(layer_map):
            raise DomainValidationError("root layer table references a missing layer")

        parent: dict[LayerId, LayerId] = {}
        for layer in normalized_layers:
            if isinstance(layer, GroupLayer):
                for child in layer.child_layer_ids:
                    if child not in layer_map:
                        raise DomainValidationError("group references a missing child")
                    if child in parent:
                        raise DomainValidationError("layer has more than one parent")
                    parent[child] = layer.layer_id

        for layer_id in layer_map:
            is_root = layer_id in root_set
            has_parent = layer_id in parent
            if is_root == has_parent:
                raise DomainValidationError(
                    "every layer must be exactly one root or one group child"
                )

        visiting: set[LayerId] = set()
        visited: set[LayerId] = set()

        def visit(layer_id: LayerId) -> None:
            if layer_id in visiting:
                raise DomainValidationError("layer graph contains a cycle")
            if layer_id in visited:
                return
            visiting.add(layer_id)
            layer = layer_map[layer_id]
            if isinstance(layer, GroupLayer):
                for child in layer.child_layer_ids:
                    visit(child)
            visiting.remove(layer_id)
            visited.add(layer_id)

        for root in self.root_layer_ids:
            visit(root)
        if visited != set(layer_map):
            raise DomainValidationError("layer graph contains unreachable values")

        asset_digests = {asset.digest for asset in normalized_assets}
        for layer in normalized_layers:
            if not set(referenced_asset_digests(layer)) <= asset_digests:
                raise DomainValidationError("layer references an undeclared asset")

        if self.selection is not None:
            if not isinstance(self.selection, Selection):
                raise DomainValidationError("selection must be typed")
            if not self.selection.bounds.is_within(self.canvas.bounds):
                raise DomainValidationError("selection bounds leave the canvas")

        if any(not isinstance(item, OperationProvenance) for item in self.provenance):
            raise DomainValidationError("document provenance contains an untyped value")
        if len(set(self.provenance)) != len(self.provenance):
            raise DomainValidationError("document provenance contains a duplicate record")

        declared_provenance = set(self.provenance)
        for layer in normalized_layers:
            operation = getattr(layer, "operation_provenance", None)
            if operation is not None and operation not in declared_provenance:
                raise DomainValidationError(
                    "generated layer provenance is absent from the document ledger"
                )
            mask = getattr(layer, "mask", None)
            if (
                mask is not None
                and mask.operation_provenance is not None
                and mask.operation_provenance not in declared_provenance
            ):
                raise DomainValidationError(
                    "operation-mask provenance is absent from the document ledger"
                )

    @property
    def layer_map(self) -> dict[LayerId, Layer]:
        return {layer.layer_id: layer for layer in self.layers}

    @property
    def asset_map(self) -> dict[ObjectId, AssetRef]:
        return {asset.digest: asset for asset in self.assets}

    @classmethod
    def from_manifest(cls, value: object) -> Self:
        required = {
            "schema",
            "documentId",
            "revisionId",
            "canvas",
            "colour",
            "engineCompatibility",
            "assets",
            "rootLayerIds",
            "layers",
            "selection",
            "provenance",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise DomainValidationError("project manifest has missing or unknown fields")
        assets = value["assets"]
        roots = value["rootLayerIds"]
        layers = value["layers"]
        provenance = value["provenance"]
        if not all(isinstance(item, list) for item in (assets, roots, layers, provenance)):
            raise DomainValidationError("project manifest tables must be lists")
        return cls(
            schema=value["schema"],
            document_id=DocumentId.parse(value["documentId"]),
            revision_id=RevisionId.parse(value["revisionId"]),
            canvas=Canvas.from_data(value["canvas"]),
            colour=ColourState.from_data(value["colour"]),
            engine_compatibility=EngineCompatibility.from_data(
                value["engineCompatibility"]
            ),
            assets=tuple(AssetRef.from_data(item) for item in assets),
            root_layer_ids=tuple(LayerId.parse(item) for item in roots),
            layers=tuple(layer_from_data(item) for item in layers),
            selection=(
                None
                if value["selection"] is None
                else Selection.from_data(value["selection"])
            ),
            provenance=tuple(OperationProvenance.from_data(item) for item in provenance),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes, *, max_manifest_bytes: int) -> Self:
        if not isinstance(payload, bytes):
            raise DomainValidationError("project manifest must be immutable bytes")
        if (
            isinstance(max_manifest_bytes, bool)
            or not isinstance(max_manifest_bytes, int)
            or max_manifest_bytes <= 0
        ):
            raise DomainValidationError("manifest byte budget must be finite and positive")
        if len(payload) > max_manifest_bytes:
            raise DomainValidationError("project manifest exceeds its byte budget")
        try:
            text = payload.decode("utf-8", errors="strict")
            value = json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainValidationError("project manifest is not strict UTF-8 JSON") from exc
        return cls.from_manifest(value)

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "documentId": self.document_id.value,
            "revisionId": self.revision_id.value,
            "canvas": self.canvas.to_data(),
            "colour": self.colour.to_data(),
            "engineCompatibility": self.engine_compatibility.to_data(),
            "assets": [asset.to_data() for asset in self.assets],
            "rootLayerIds": [layer_id.value for layer_id in self.root_layer_ids],
            "layers": [layer_to_data(layer) for layer in self.layers],
            "selection": None if self.selection is None else self.selection.to_data(),
            "provenance": [item.to_data() for item in self.provenance],
        }

    def canonical_bytes(self) -> bytes:
        try:
            text = json.dumps(
                self.to_manifest(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return (text + "\n").encode("utf-8", errors="strict")
        except (TypeError, UnicodeEncodeError, ValueError) as exc:
            raise DomainValidationError("project cannot be serialized canonically") from exc

    @property
    def manifest_digest(self) -> ObjectId:
        return ObjectId(hashlib.sha256(self.canonical_bytes()).hexdigest())
