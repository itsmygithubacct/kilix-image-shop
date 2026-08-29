"""Editable text values with pinned font, axes, fallback, and preview identity."""

from __future__ import annotations

from dataclasses import dataclass

from kilix_image_shop.domain.commands import AddLayer, EditText
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import (
    FontAxis,
    FontFallback,
    TextLayer,
    TextLayout,
)


class TextValidationError(ValueError):
    """Editable text or font payload identity is invalid."""


@dataclass(frozen=True, slots=True)
class EditableText:
    text: str
    layout: TextLayout
    font_digest: ObjectId
    face_index: int
    axes: tuple[FontAxis, ...]
    fallbacks: tuple[FontFallback, ...]
    preview_asset_digest: ObjectId

    def __post_init__(self) -> None:
        # Reuse the closed domain validator without retaining a synthetic layer.
        self.to_layer(
            LayerId("00000000-0000-4000-8000-000000000000"),
            "validation",
        )

    def to_layer(self, layer_id: LayerId, name: str) -> TextLayer:
        return TextLayer(
            layer_id=layer_id,
            name=name,
            text=self.text,
            layout=self.layout,
            font_digest=self.font_digest,
            face_index=self.face_index,
            axes=self.axes,
            fallbacks=self.fallbacks,
            preview_asset_digest=self.preview_asset_digest,
        )


def font_digest(font_payload: bytes) -> ObjectId:
    if not isinstance(font_payload, bytes) or not font_payload:
        raise TextValidationError("font payload must be non-empty immutable bytes")
    return ObjectId.from_bytes(font_payload)


def validate_font_payload(font_payload: bytes, expected_digest: ObjectId) -> None:
    if font_digest(font_payload) != expected_digest:
        raise TextValidationError("font payload differs from its pinned digest")


def add_text_layer(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    name: str,
    editable: EditableText,
    parent_id: LayerId | None,
    index: int,
) -> AddLayer:
    if editable.preview_asset_digest not in state.asset_map:
        raise TextValidationError("text preview asset is not declared")
    return AddLayer(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer=editable.to_layer(layer_id, name),
        parent_id=parent_id,
        index=index,
    )


def edit_text_layer(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    editable: EditableText,
) -> EditText:
    if not isinstance(state.layer_map.get(layer_id), TextLayer):
        raise TextValidationError("text edit target is not a text layer")
    if editable.preview_asset_digest not in state.asset_map:
        raise TextValidationError("text preview asset is not declared")
    return EditText(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        text=editable.text,
        layout=editable.layout,
        font_digest=editable.font_digest,
        face_index=editable.face_index,
        axes=editable.axes,
        fallbacks=editable.fallbacks,
        preview_asset_digest=editable.preview_asset_digest,
    )
