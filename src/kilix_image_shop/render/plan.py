"""Pure render-plan derivation from one immutable document revision."""

from __future__ import annotations

import json
from dataclasses import dataclass

from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import (
    AdjustmentLayer,
    GroupLayer,
    Layer,
    MaskObject,
    PixelLayer,
    TextLayer,
)
from kilix_image_shop.engine.api import (
    AdjustmentParameters,
    AffineTransformCropParameters,
    ColourConversionParameters,
    DestinationCropScaleParameters,
    GraphNodeKind,
    GraphNodeSpec,
    GraphSpec,
    InvalidGraph,
    MaskParameters,
    OpacityBlendParameters,
    OrderedGroupParameters,
    PixelFormat,
    PixelSourceParameters,
    PixelSpec,
    TextSourceParameters,
)

from .graph import GraphDependency


@dataclass(frozen=True, slots=True)
class RenderPlan:
    graph: GraphSpec
    output_bounds: Rect
    layer_ids: tuple[LayerId, ...]
    object_ids: tuple[ObjectId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.graph, GraphSpec) or not isinstance(
            self.output_bounds, Rect
        ):
            raise InvalidGraph("render plan requires graph and checked output geometry")
        layer_values = tuple(item.value for item in self.layer_ids)
        object_values = tuple(item.value for item in self.object_ids)
        if layer_values != tuple(sorted(set(layer_values))) or object_values != tuple(
            sorted(set(object_values))
        ):
            raise InvalidGraph("render plan dependencies must be sorted and unique")
        destination = self.graph.nodes[-1].parameters
        if not isinstance(destination, DestinationCropScaleParameters) or (
            destination.source != self.output_bounds
            or destination.destination != self.output_bounds
        ):
            raise InvalidGraph("render plan output differs from its destination node")

    @property
    def digest(self) -> ObjectId:
        """The graph digest is the executable render-plan identity."""

        return self.graph.digest

    @property
    def output_spec(self) -> PixelSpec:
        return self.graph.output_spec

    @property
    def revision(self) -> RevisionId:
        return self.graph.revision

    @property
    def compatibility_digest(self) -> ObjectId:
        return self.graph.compatibility_digest

    @property
    def dependency(self) -> GraphDependency:
        return GraphDependency(
            self.digest,
            self.revision,
            self.layer_ids,
            self.object_ids,
            self.output_bounds,
        )


class _PlanBuilder:
    def __init__(self, document: DocumentState, output_bounds: Rect) -> None:
        self.document = document
        self.output_bounds = output_bounds
        try:
            working_format = PixelFormat(document.engine_compatibility.working_format)
        except ValueError as exc:
            raise InvalidGraph("document working format is unsupported") from exc
        self.working_spec = PixelSpec.colour(
            working_format,
            document.colour.working_profile,
            alpha_association=document.engine_compatibility.alpha_association,
        )
        self.nodes: list[GraphNodeSpec] = []
        self.layers: set[LayerId] = set()
        self.objects: set[ObjectId] = {document.colour.working_profile}
        self._sequence = 0
        self._halos = dict(document.engine_compatibility.tile_halos)

    def _halo(self, *families: str) -> int:
        fallback = self._halos.get("default", 0)
        return max((self._halos.get(item, fallback) for item in families), default=0)

    def _add(
        self,
        kind: GraphNodeKind,
        inputs: tuple[str, ...],
        parameters: object,
        spec: PixelSpec,
        *,
        halo: int = 0,
    ) -> str:
        self._sequence += 1
        node_id = f"n{self._sequence:06d}"
        self.nodes.append(
            GraphNodeSpec(node_id, kind, inputs, parameters, spec, halo_pixels=halo)
        )
        return node_id

    def _source_spec(self, profile: ObjectId) -> PixelSpec:
        return PixelSpec.colour(
            self.working_spec.pixel_format,
            profile,
            alpha_association=self.working_spec.alpha_association,
        )

    def _convert(self, source: str, profile: ObjectId) -> str:
        self.objects.add(profile)
        if profile == self.document.colour.working_profile:
            return source
        return self._add(
            GraphNodeKind.COLOUR_CONVERSION,
            (source,),
            ColourConversionParameters(
                profile,
                self.document.colour.working_profile,
                self.document.colour.conversion_policy,
            ),
            self.working_spec,
            halo=self._halo("colour"),
        )

    def _mask(self, source: str, mask: MaskObject | None) -> str:
        if mask is None:
            return source
        self.objects.add(mask.object_id)
        if mask.source_ref is not None:
            self.objects.add(mask.source_ref)
        mask_extent = Rect(
            mask.origin_x,
            mask.origin_y,
            mask.width,
            mask.height,
        )
        mask_source = self._add(
            GraphNodeKind.PIXEL_SOURCE,
            (),
            PixelSourceParameters(mask.object_id, mask_extent),
            PixelSpec.foreground_mask(),
            halo=self._halo("source"),
        )
        return self._add(
            GraphNodeKind.MASK,
            (source, mask_source),
            MaskParameters(),
            self.working_spec,
            halo=self._halo("mask"),
        )

    def _transform(self, source: str, layer: PixelLayer | TextLayer | GroupLayer) -> str:
        return self._add(
            GraphNodeKind.AFFINE_TRANSFORM_CROP,
            (source,),
            AffineTransformCropParameters(layer.transform, self.output_bounds),
            self.working_spec,
            halo=self._halo("transform"),
        )

    @staticmethod
    def _text_digest(layer: TextLayer) -> ObjectId:
        value = {
            "axes": [item.to_data() for item in layer.axes],
            "faceIndex": layer.face_index,
            "fallbacks": [item.to_data() for item in layer.fallbacks],
            "fontSha256": layer.font_digest.value,
            "layout": layer.layout.to_data(),
            "text": layer.text,
        }
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return ObjectId.from_bytes(payload)

    def _pixel(self, layer: PixelLayer) -> str:
        asset = self.document.asset_map[layer.asset_digest]
        self.objects.add(asset.digest)
        source_spec = self._source_spec(asset.profile_digest)
        source = self._add(
            GraphNodeKind.PIXEL_SOURCE,
            (),
            PixelSourceParameters(
                asset.digest,
                Rect(0, 0, asset.width, asset.height),
            ),
            source_spec,
            halo=self._halo("source"),
        )
        source = self._convert(source, asset.profile_digest)
        source = self._mask(source, layer.mask)
        return self._transform(source, layer)

    def _text(self, layer: TextLayer) -> str:
        asset = self.document.asset_map[layer.preview_asset_digest]
        self.objects.update((layer.preview_asset_digest, layer.font_digest))
        self.objects.update(
            item.resolved_font_digest
            for item in layer.fallbacks
            if item.resolved_font_digest is not None
        )
        source_spec = self._source_spec(asset.profile_digest)
        source = self._add(
            GraphNodeKind.TEXT_SOURCE,
            (),
            TextSourceParameters(
                self._text_digest(layer),
                layer.font_digest,
                layer.preview_asset_digest,
                Rect(0, 0, layer.layout.width, layer.layout.height),
            ),
            source_spec,
            halo=self._halo("text"),
        )
        source = self._convert(source, asset.profile_digest)
        source = self._mask(source, layer.mask)
        return self._transform(source, layer)

    def _group(self, layer: GroupLayer) -> str | None:
        source = self._compose(layer.child_layer_ids)
        if source is None:
            return None
        source = self._add(
            GraphNodeKind.ORDERED_GROUP,
            (source,),
            OrderedGroupParameters(),
            self.working_spec,
            halo=self._halo("group"),
        )
        source = self._mask(source, layer.mask)
        return self._transform(source, layer)

    def _content(self, layer: Layer) -> str | None:
        if isinstance(layer, PixelLayer):
            return self._pixel(layer)
        if isinstance(layer, TextLayer):
            return self._text(layer)
        if isinstance(layer, GroupLayer):
            return self._group(layer)
        return None

    def _blend(self, backdrop: str | None, content: str, layer: Layer) -> str:
        inputs = (content,) if backdrop is None else (backdrop, content)
        families = ("blend",) if backdrop is None else ("blend", layer.blend_mode.value)
        return self._add(
            GraphNodeKind.OPACITY_BLEND,
            inputs,
            OpacityBlendParameters(layer.opacity_u16, layer.blend_mode),
            self.working_spec,
            halo=self._halo(*families),
        )

    def _adjust(
        self,
        backdrop: str,
        layer: AdjustmentLayer,
    ) -> str:
        adjusted = self._add(
            GraphNodeKind.ADJUSTMENT,
            (backdrop,),
            AdjustmentParameters(layer.adjustment),
            self.working_spec,
            halo=self._halo("adjustment", layer.adjustment.adjustment_id.value),
        )
        adjusted = self._mask(adjusted, layer.mask)
        return self._blend(backdrop, adjusted, layer)

    def _compose(self, layer_ids: tuple[LayerId, ...]) -> str | None:
        backdrop: str | None = None
        for layer_id in layer_ids:
            layer = self.document.layer_map[layer_id]
            if not layer.visible:
                continue
            self.layers.add(layer_id)
            if isinstance(layer, AdjustmentLayer):
                if backdrop is not None:
                    backdrop = self._adjust(backdrop, layer)
                continue
            content = self._content(layer)
            if content is not None:
                backdrop = self._blend(backdrop, content, layer)
        return backdrop

    def build(self) -> RenderPlan:
        output = self._compose(self.document.root_layer_ids)
        if output is None:
            raise InvalidGraph("document has no visible renderable layer")
        output = self._add(
            GraphNodeKind.ORDERED_GROUP,
            (output,),
            OrderedGroupParameters(),
            self.working_spec,
            halo=self._halo("group"),
        )
        destination = self._add(
            GraphNodeKind.DESTINATION_CROP_SCALE,
            (output,),
            DestinationCropScaleParameters(self.output_bounds, self.output_bounds),
            self.working_spec,
            halo=self._halo("destination"),
        )
        graph = GraphSpec(
            self.document.revision_id,
            self.document.engine_compatibility.digest,
            tuple(self.nodes),
            destination,
        )
        return RenderPlan(
            graph,
            self.output_bounds,
            tuple(sorted(self.layers, key=lambda item: item.value)),
            tuple(sorted(self.objects, key=lambda item: item.value)),
        )


def derive_render_plan(
    document: DocumentState,
    *,
    output_bounds: Rect | None = None,
) -> RenderPlan:
    """Project visible immutable document semantics into one closed graph."""

    if not isinstance(document, DocumentState):
        raise InvalidGraph("render planning requires an immutable document state")
    bounds = document.canvas.bounds if output_bounds is None else output_bounds
    if not isinstance(bounds, Rect) or not bounds.is_within(document.canvas.bounds):
        raise InvalidGraph("render output bounds must stay within the document canvas")
    return _PlanBuilder(document, bounds).build()


__all__ = ("RenderPlan", "derive_render_plan")
