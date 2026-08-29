"""Vector/raster selection values, commands, and lossless mask conversion."""

from __future__ import annotations

from kilix_image_shop.domain.commands import SetSelection
from kilix_image_shop.domain.document import DocumentState
from kilix_image_shop.domain.geometry import Rect
from kilix_image_shop.domain.identifiers import ObjectId, RevisionId
from kilix_image_shop.domain.layers import MaskObject, MaskSource, Selection, SelectionKind


class SelectionValidationError(ValueError):
    """A selection edit or conversion is invalid."""


def make_selection(
    kind: SelectionKind,
    object_id: ObjectId,
    bounds: Rect,
) -> Selection:
    if not isinstance(kind, SelectionKind):
        raise SelectionValidationError("selection kind must be closed")
    return Selection(kind, object_id, bounds)


def set_selection(
    state: DocumentState,
    *,
    new_revision: RevisionId,
    selection: Selection,
) -> SetSelection:
    if not isinstance(selection, Selection) or not selection.bounds.is_within(
        state.canvas.bounds
    ):
        raise SelectionValidationError("selection must stay within the document canvas")
    return SetSelection(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        selection=selection,
    )


def clear_selection(
    state: DocumentState,
    *,
    new_revision: RevisionId,
) -> SetSelection:
    return SetSelection(
        expected_revision=state.revision_id,
        new_revision=new_revision,
        selection=None,
    )


def selection_to_mask(selection: Selection, mask_object_id: ObjectId) -> MaskObject:
    """Bind already rasterized selection samples without changing any sample."""

    if not isinstance(selection, Selection) or not isinstance(mask_object_id, ObjectId):
        raise SelectionValidationError("selection conversion inputs are malformed")
    return MaskObject(
        object_id=mask_object_id,
        width=selection.bounds.width,
        height=selection.bounds.height,
        origin_x=selection.bounds.x,
        origin_y=selection.bounds.y,
        source=MaskSource.SELECTION,
        source_ref=selection.object_id,
    )
