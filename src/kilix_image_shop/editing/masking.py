"""Mask paint, attach, replacement, and removal command builders."""

from __future__ import annotations

from kilix_image_shop.domain.commands import AttachMask, PaintMask
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import MaskObject, MaskSource


class MaskingValidationError(ValueError):
    """A mask command would violate the editable-mask contract."""


def _target(state: DocumentState, layer_id: LayerId) -> None:
    if not isinstance(state, DocumentState) or layer_id not in state.layer_map:
        raise MaskingValidationError("mask target layer does not exist")


def attach_or_replace_mask(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    mask: MaskObject,
) -> AttachMask:
    _target(state, layer_id)
    if not isinstance(mask, MaskObject):
        raise MaskingValidationError("attached mask must be typed")
    return AttachMask(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        mask=mask,
    )


def remove_mask(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
) -> AttachMask:
    _target(state, layer_id)
    if getattr(state.layer_map[layer_id], "mask", None) is None:
        raise MaskingValidationError("mask target has no mask to remove")
    return AttachMask(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        mask=None,
    )


def paint_mask(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    layer_id: LayerId,
    mask: MaskObject,
    changed_tile_refs: tuple[ObjectId, ...],
) -> PaintMask:
    _target(state, layer_id)
    if not isinstance(mask, MaskObject) or mask.source is not MaskSource.HAND_PAINTED:
        raise MaskingValidationError("mask paint requires a hand-painted mask object")
    if not isinstance(changed_tile_refs, tuple) or not changed_tile_refs:
        raise MaskingValidationError("mask paint requires at least one changed tile")
    if any(not isinstance(item, ObjectId) for item in changed_tile_refs):
        raise MaskingValidationError("mask paint tile refs must be typed")
    normalized = tuple(sorted(set(changed_tile_refs), key=lambda item: item.value))
    if len(normalized) != len(changed_tile_refs):
        raise MaskingValidationError("mask paint tile refs cannot repeat")
    return PaintMask(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        layer_id=layer_id,
        mask=mask,
        changed_tile_refs=normalized,
    )
