"""Pure revision-checked commands and immutable reduction results."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeAlias

from .assets import AssetRef, ImportPolicy
from .document import DocumentState
from .geometry import AffineTransform, Canvas
from .identifiers import DomainValidationError, LayerId, ObjectId, RevisionId
from .layers import (
    Adjustment,
    AdjustmentLayer,
    BlendMode,
    GroupLayer,
    Layer,
    MaskObject,
    MaskSource,
    OperationProvenance,
    PixelLayer,
    Selection,
    TextLayer,
    TextLayout,
    FontAxis,
    FontFallback,
)


class RevisionConflict(DomainValidationError):
    """A command targeted a revision that is no longer current."""


class CommandValidationError(DomainValidationError):
    """A command is structurally valid Python but invalid for this document."""


class EffectKind(StrEnum):
    WRITE_OBJECT = "write-object"
    INVALIDATE_PROXY = "invalidate-proxy"
    SCHEDULE_RENDER = "schedule-render"


@dataclass(frozen=True, slots=True)
class ResolvedObject:
    object_id: ObjectId
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, ObjectId):
            raise CommandValidationError("resolved object identity must be typed")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count <= 0
        ):
            raise CommandValidationError("resolved object size must be positive")


@dataclass(frozen=True, slots=True)
class ReductionContext:
    objects: tuple[ResolvedObject, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.objects, tuple) or any(
            not isinstance(item, ResolvedObject) for item in self.objects
        ):
            raise CommandValidationError("resolved objects must be an immutable tuple")
        if len({item.object_id for item in self.objects}) != len(self.objects):
            raise CommandValidationError("resolved object table contains a duplicate")

    @property
    def object_map(self) -> dict[ObjectId, ResolvedObject]:
        return {item.object_id: item for item in self.objects}


@dataclass(frozen=True, slots=True)
class CommandEffect:
    kind: EffectKind
    object_id: ObjectId | None = None
    byte_count: int | None = None
    layer_ids: tuple[LayerId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EffectKind):
            raise CommandValidationError("effect kind must be closed")
        if any(not isinstance(item, LayerId) for item in self.layer_ids):
            raise CommandValidationError("effect layer IDs must be typed")
        if self.kind is EffectKind.WRITE_OBJECT:
            if not isinstance(self.object_id, ObjectId):
                raise CommandValidationError("object-write effect needs an object ID")
            if (
                isinstance(self.byte_count, bool)
                or not isinstance(self.byte_count, int)
                or self.byte_count <= 0
            ):
                raise CommandValidationError("object-write effect needs a positive size")
        elif self.object_id is not None or self.byte_count is not None:
            raise CommandValidationError("non-write effect cannot carry object metadata")


@dataclass(frozen=True, slots=True)
class ReductionResult:
    before_revision: RevisionId
    state: DocumentState
    effects: tuple[CommandEffect, ...]
    changed_layer_ids: tuple[LayerId, ...]

    def __post_init__(self) -> None:
        if self.before_revision == self.state.revision_id:
            raise CommandValidationError("reduction must publish a distinct revision")
        if not isinstance(self.effects, tuple) or not isinstance(
            self.changed_layer_ids, tuple
        ):
            raise CommandValidationError("reduction collections must be immutable")


@dataclass(frozen=True, slots=True, kw_only=True)
class AddLayer:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer: Layer
    parent_id: LayerId | None
    index: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoveLayer:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    recursive: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ReorderLayer:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    parent_id: LayerId | None
    index: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SetLayerProperty:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    name: str | None = None
    visible: bool | None = None
    opacity_u16: int | None = None
    blend_mode: BlendMode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeAdjustment:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    adjustment: Adjustment


@dataclass(frozen=True, slots=True, kw_only=True)
class SetTransform:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    transform: AffineTransform


@dataclass(frozen=True, slots=True, kw_only=True)
class CropCanvas:
    expected_revision: RevisionId
    new_revision: RevisionId
    canvas: Canvas


@dataclass(frozen=True, slots=True, kw_only=True)
class SetSelection:
    expected_revision: RevisionId
    new_revision: RevisionId
    selection: Selection | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PaintMask:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    mask: MaskObject
    changed_tile_refs: tuple[ObjectId, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachMask:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    mask: MaskObject | None


@dataclass(frozen=True, slots=True, kw_only=True)
class EditText:
    expected_revision: RevisionId
    new_revision: RevisionId
    layer_id: LayerId
    text: str
    layout: TextLayout
    font_digest: ObjectId
    face_index: int
    axes: tuple[FontAxis, ...]
    fallbacks: tuple[FontFallback, ...]
    preview_asset_digest: ObjectId


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportAsset:
    expected_revision: RevisionId
    new_revision: RevisionId
    asset: AssetRef
    layer: PixelLayer
    parent_id: LayerId | None
    index: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FlattenLayers:
    expected_revision: RevisionId
    new_revision: RevisionId
    source_layer_ids: tuple[LayerId, ...]
    output_asset: AssetRef
    output_layer: PixelLayer


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyOperationOutput:
    expected_revision: RevisionId
    new_revision: RevisionId
    provenance: OperationProvenance
    output_asset: AssetRef | None = None
    output_layer: PixelLayer | None = None
    target_layer_id: LayerId | None = None
    output_mask: MaskObject | None = None
    parent_id: LayerId | None = None
    index: int = 0


Command: TypeAlias = (
    AddLayer
    | RemoveLayer
    | ReorderLayer
    | SetLayerProperty
    | ChangeAdjustment
    | SetTransform
    | CropCanvas
    | SetSelection
    | PaintMask
    | AttachMask
    | EditText
    | ImportAsset
    | FlattenLayers
    | ApplyOperationOutput
)


COMMAND_TYPES: tuple[type[object], ...] = (
    AddLayer,
    RemoveLayer,
    ReorderLayer,
    SetLayerProperty,
    ChangeAdjustment,
    SetTransform,
    CropCanvas,
    SetSelection,
    PaintMask,
    AttachMask,
    EditText,
    ImportAsset,
    FlattenLayers,
    ApplyOperationOutput,
)


def _check_revision(state: DocumentState, command: Command) -> None:
    if not isinstance(command.expected_revision, RevisionId) or not isinstance(
        command.new_revision, RevisionId
    ):
        raise CommandValidationError("command revisions must be typed")
    if command.expected_revision != state.revision_id:
        raise RevisionConflict(
            f"expected revision {command.expected_revision}; current is {state.revision_id}"
        )
    if command.new_revision == state.revision_id:
        raise CommandValidationError("new revision must differ from the current revision")


def _index(value: object, length: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandValidationError("layer index must be an integer")
    if not 0 <= value <= length:
        raise CommandValidationError("layer index leaves its target container")
    return value


def _layer_parent_map(state: DocumentState) -> dict[LayerId, LayerId | None]:
    result: dict[LayerId, LayerId | None] = {
        layer_id: None for layer_id in state.root_layer_ids
    }
    for layer in state.layers:
        if isinstance(layer, GroupLayer):
            for child in layer.child_layer_ids:
                result[child] = layer.layer_id
    return result


def _children(
    state: DocumentState, parent_id: LayerId | None
) -> tuple[LayerId, ...]:
    if parent_id is None:
        return state.root_layer_ids
    parent = state.layer_map.get(parent_id)
    if not isinstance(parent, GroupLayer):
        raise CommandValidationError("target parent is not a group")
    return parent.child_layer_ids


def _replace_children(
    state: DocumentState,
    parent_id: LayerId | None,
    children: tuple[LayerId, ...],
    layers: dict[LayerId, Layer],
) -> tuple[LayerId, ...]:
    if parent_id is None:
        return children
    parent = layers.get(parent_id)
    if not isinstance(parent, GroupLayer):
        raise CommandValidationError("target parent is not a group")
    layers[parent_id] = replace(parent, child_layer_ids=children)
    return state.root_layer_ids


def _known_object_ids(state: DocumentState) -> set[ObjectId]:
    known = {asset.digest for asset in state.assets}
    if state.selection is not None:
        known.add(state.selection.object_id)
    for layer in state.layers:
        mask = getattr(layer, "mask", None)
        if mask is not None:
            known.add(mask.object_id)
            if mask.source_ref is not None:
                known.add(mask.source_ref)
    return known


def _require_resolved(
    state: DocumentState,
    context: ReductionContext,
    object_id: ObjectId,
    byte_count: int,
) -> CommandEffect | None:
    if object_id in _known_object_ids(state):
        return None
    resolved = context.object_map.get(object_id)
    if resolved is None:
        raise CommandValidationError(f"object {object_id} has no resolved local metadata")
    if resolved.byte_count != byte_count:
        raise CommandValidationError(f"object {object_id} byte count does not match")
    return CommandEffect(
        EffectKind.WRITE_OBJECT,
        object_id=object_id,
        byte_count=byte_count,
    )


def _require_reference(
    state: DocumentState,
    context: ReductionContext,
    object_id: ObjectId,
) -> None:
    if object_id not in _known_object_ids(state) and object_id not in context.object_map:
        raise CommandValidationError(
            f"referenced object {object_id} has no resolved local metadata"
        )


def _mask_effect(
    state: DocumentState, context: ReductionContext, mask: MaskObject | None
) -> tuple[CommandEffect, ...]:
    if mask is None:
        return ()
    effects: list[CommandEffect] = []
    mask_write = _require_resolved(
        state,
        context,
        mask.object_id,
        mask.width * mask.height,
    )
    if mask_write is not None:
        effects.append(mask_write)
    if mask.source_ref is not None and mask.source_ref not in _known_object_ids(state):
        resolved = context.object_map.get(mask.source_ref)
        if resolved is None:
            raise CommandValidationError(
                f"mask source {mask.source_ref} has no resolved local metadata"
            )
        effects.append(
            CommandEffect(
                EffectKind.WRITE_OBJECT,
                object_id=resolved.object_id,
                byte_count=resolved.byte_count,
            )
        )
    return tuple(effects)


def _render_effects(changed: tuple[LayerId, ...]) -> tuple[CommandEffect, ...]:
    return (
        CommandEffect(EffectKind.INVALIDATE_PROXY, layer_ids=changed),
        CommandEffect(EffectKind.SCHEDULE_RENDER, layer_ids=changed),
    )


def _document(
    state: DocumentState,
    revision: RevisionId,
    *,
    layers: dict[LayerId, Layer] | None = None,
    roots: tuple[LayerId, ...] | None = None,
    assets: tuple[AssetRef, ...] | None = None,
    canvas: Canvas | None = None,
    selection: Selection | None | object = ...,
    provenance: tuple[OperationProvenance, ...] | None = None,
) -> DocumentState:
    kwargs: dict[str, object] = {
        "revision_id": revision,
        "layers": tuple((state.layer_map if layers is None else layers).values()),
        "root_layer_ids": state.root_layer_ids if roots is None else roots,
        "assets": state.assets if assets is None else assets,
        "canvas": state.canvas if canvas is None else canvas,
        "provenance": state.provenance if provenance is None else provenance,
    }
    if selection is not ...:
        kwargs["selection"] = selection
    return replace(state, **kwargs)


def _insert_layer(
    state: DocumentState,
    layer: Layer,
    parent_id: LayerId | None,
    index: int,
) -> tuple[dict[LayerId, Layer], tuple[LayerId, ...]]:
    layers = state.layer_map
    if layer.layer_id in layers:
        raise CommandValidationError("layer ID is already present")
    children = _children(state, parent_id)
    position = _index(index, len(children))
    layers[layer.layer_id] = layer
    updated = children[:position] + (layer.layer_id,) + children[position:]
    roots = _replace_children(state, parent_id, updated, layers)
    return layers, roots


def _descendants(layer_id: LayerId, layers: dict[LayerId, Layer]) -> tuple[LayerId, ...]:
    found: list[LayerId] = []

    def walk(candidate: LayerId) -> None:
        found.append(candidate)
        layer = layers[candidate]
        if isinstance(layer, GroupLayer):
            for child in layer.child_layer_ids:
                walk(child)

    walk(layer_id)
    return tuple(found)


def _remove_layer(
    state: DocumentState,
    layer_id: LayerId,
    *,
    recursive: bool,
) -> tuple[dict[LayerId, Layer], tuple[LayerId, ...], tuple[LayerId, ...], LayerId | None, int]:
    layers = state.layer_map
    layer = layers.get(layer_id)
    if layer is None:
        raise CommandValidationError("layer does not exist")
    if isinstance(layer, GroupLayer) and layer.child_layer_ids and not recursive:
        raise CommandValidationError("non-empty group removal must be recursive")
    parents = _layer_parent_map(state)
    parent_id = parents[layer_id]
    siblings = _children(state, parent_id)
    index = siblings.index(layer_id)
    updated_siblings = siblings[:index] + siblings[index + 1 :]
    removed = _descendants(layer_id, layers)
    for candidate in removed:
        del layers[candidate]
    roots = _replace_children(state, parent_id, updated_siblings, layers)
    return layers, roots, removed, parent_id, index


def _used_provenance(layers: dict[LayerId, Layer]) -> set[OperationProvenance]:
    used: set[OperationProvenance] = set()
    for layer in layers.values():
        operation = getattr(layer, "operation_provenance", None)
        if operation is not None:
            used.add(operation)
        mask = getattr(layer, "mask", None)
        if mask is not None and mask.operation_provenance is not None:
            used.add(mask.operation_provenance)
    return used


def reduce_command(
    state: DocumentState,
    command: Command,
    context: ReductionContext = ReductionContext(),
) -> ReductionResult:
    """Validate and reduce one command without filesystem or native side effects."""

    if not isinstance(state, DocumentState):
        raise CommandValidationError("command state must be a DocumentState")
    if type(command) not in COMMAND_TYPES:
        raise CommandValidationError("unsupported command type")
    if not isinstance(context, ReductionContext):
        raise CommandValidationError("command context must be typed")
    _check_revision(state, command)

    effects: list[CommandEffect] = []
    changed: tuple[LayerId, ...]

    if isinstance(command, AddLayer):
        layers, roots = _insert_layer(
            state, command.layer, command.parent_id, command.index
        )
        effects.extend(_mask_effect(state, context, getattr(command.layer, "mask", None)))
        candidate = _document(state, command.new_revision, layers=layers, roots=roots)
        changed = (command.layer.layer_id,)

    elif isinstance(command, RemoveLayer):
        layers, roots, removed, _, _ = _remove_layer(
            state, command.layer_id, recursive=command.recursive
        )
        used = _used_provenance(layers)
        candidate = _document(
            state,
            command.new_revision,
            layers=layers,
            roots=roots,
            provenance=tuple(item for item in state.provenance if item in used),
        )
        changed = removed

    elif isinstance(command, ReorderLayer):
        if command.layer_id not in state.layer_map:
            raise CommandValidationError("layer does not exist")
        layers = state.layer_map
        parents = _layer_parent_map(state)
        old_parent = parents[command.layer_id]
        old_siblings = _children(state, old_parent)
        old_index = old_siblings.index(command.layer_id)
        without = old_siblings[:old_index] + old_siblings[old_index + 1 :]
        roots = _replace_children(state, old_parent, without, layers)

        if command.parent_id == old_parent:
            target = without
        elif command.parent_id is None:
            target = roots
        else:
            parent = layers.get(command.parent_id)
            if not isinstance(parent, GroupLayer):
                raise CommandValidationError("target parent is not a group")
            target = parent.child_layer_ids
        position = _index(command.index, len(target))
        target = target[:position] + (command.layer_id,) + target[position:]
        roots = _replace_children(state, command.parent_id, target, layers)
        candidate = _document(state, command.new_revision, layers=layers, roots=roots)
        changed = (command.layer_id,)

    elif isinstance(command, SetLayerProperty):
        if all(
            value is None
            for value in (
                command.name,
                command.visible,
                command.opacity_u16,
                command.blend_mode,
            )
        ):
            raise CommandValidationError("layer-property command changes no property")
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if layer is None:
            raise CommandValidationError("layer does not exist")
        updates: dict[str, object] = {}
        for field in ("name", "visible", "opacity_u16", "blend_mode"):
            value = getattr(command, field)
            if value is not None:
                updates[field] = value
        layers[command.layer_id] = replace(layer, **updates)
        candidate = _document(state, command.new_revision, layers=layers)
        changed = (command.layer_id,)

    elif isinstance(command, ChangeAdjustment):
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if not isinstance(layer, AdjustmentLayer):
            raise CommandValidationError("target layer is not an adjustment")
        layers[command.layer_id] = replace(layer, adjustment=command.adjustment)
        candidate = _document(state, command.new_revision, layers=layers)
        changed = (command.layer_id,)

    elif isinstance(command, SetTransform):
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if not isinstance(layer, (PixelLayer, TextLayer, GroupLayer)):
            raise CommandValidationError("target layer has no transform")
        layers[command.layer_id] = replace(layer, transform=command.transform)
        candidate = _document(state, command.new_revision, layers=layers)
        changed = (command.layer_id,)

    elif isinstance(command, CropCanvas):
        candidate = _document(state, command.new_revision, canvas=command.canvas)
        changed = state.root_layer_ids

    elif isinstance(command, SetSelection):
        if command.selection is not None:
            resolved = context.object_map.get(command.selection.object_id)
            if (
                command.selection.object_id not in _known_object_ids(state)
                and resolved is None
            ):
                raise CommandValidationError(
                    "selection object has no resolved local metadata"
                )
            if resolved is not None and command.selection.object_id not in _known_object_ids(
                state
            ):
                effects.append(
                    CommandEffect(
                        EffectKind.WRITE_OBJECT,
                        object_id=resolved.object_id,
                        byte_count=resolved.byte_count,
                    )
                )
        candidate = _document(
            state, command.new_revision, selection=command.selection
        )
        changed = ()

    elif isinstance(command, PaintMask):
        if command.mask.source is not MaskSource.HAND_PAINTED:
            raise CommandValidationError("paint command requires a hand-painted mask")
        if not isinstance(command.changed_tile_refs, tuple) or not command.changed_tile_refs:
            raise CommandValidationError("paint command requires changed tile refs")
        if any(not isinstance(item, ObjectId) for item in command.changed_tile_refs):
            raise CommandValidationError("changed tile refs must be typed")
        for item in command.changed_tile_refs:
            _require_reference(state, context, item)
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if layer is None:
            raise CommandValidationError("layer does not exist")
        effects.extend(_mask_effect(state, context, command.mask))
        layers[command.layer_id] = replace(layer, mask=command.mask)
        candidate = _document(state, command.new_revision, layers=layers)
        changed = (command.layer_id,)

    elif isinstance(command, AttachMask):
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if layer is None:
            raise CommandValidationError("layer does not exist")
        effects.extend(_mask_effect(state, context, command.mask))
        layers[command.layer_id] = replace(layer, mask=command.mask)
        provenance = list(state.provenance)
        if (
            command.mask is not None
            and command.mask.operation_provenance is not None
            and command.mask.operation_provenance not in provenance
        ):
            provenance.append(command.mask.operation_provenance)
        candidate = _document(
            state,
            command.new_revision,
            layers=layers,
            provenance=tuple(provenance),
        )
        changed = (command.layer_id,)

    elif isinstance(command, EditText):
        layers = state.layer_map
        layer = layers.get(command.layer_id)
        if not isinstance(layer, TextLayer):
            raise CommandValidationError("target layer is not text")
        if command.preview_asset_digest not in state.asset_map:
            raise CommandValidationError("text preview asset is undeclared")
        layers[command.layer_id] = replace(
            layer,
            text=command.text,
            layout=command.layout,
            font_digest=command.font_digest,
            face_index=command.face_index,
            axes=command.axes,
            fallbacks=command.fallbacks,
            preview_asset_digest=command.preview_asset_digest,
        )
        candidate = _document(state, command.new_revision, layers=layers)
        changed = (command.layer_id,)

    elif isinstance(command, ImportAsset):
        if command.layer.asset_digest != command.asset.digest:
            raise CommandValidationError("import layer and asset digests disagree")
        if command.asset.digest in state.asset_map:
            raise CommandValidationError("asset digest is already declared")
        if command.asset.import_policy is ImportPolicy.COPIED:
            effect = _require_resolved(
                state, context, command.asset.digest, command.asset.byte_count
            )
            if effect is not None:
                effects.append(effect)
        layers, roots = _insert_layer(
            state, command.layer, command.parent_id, command.index
        )
        candidate = _document(
            state,
            command.new_revision,
            layers=layers,
            roots=roots,
            assets=state.assets + (command.asset,),
        )
        changed = (command.layer.layer_id,)

    elif isinstance(command, FlattenLayers):
        if not command.source_layer_ids or len(set(command.source_layer_ids)) != len(
            command.source_layer_ids
        ):
            raise CommandValidationError("flatten sources must be a non-empty unique tuple")
        if command.output_layer.asset_digest != command.output_asset.digest:
            raise CommandValidationError("flatten layer and asset digests disagree")
        if command.output_asset.import_policy is not ImportPolicy.COPIED:
            raise CommandValidationError("flatten output must be project-owned")
        if command.output_asset.digest in state.asset_map:
            raise CommandValidationError("flatten output asset already exists")
        parents = _layer_parent_map(state)
        try:
            parent_ids = {parents[item] for item in command.source_layer_ids}
        except KeyError as exc:
            raise CommandValidationError("flatten source layer does not exist") from exc
        if len(parent_ids) != 1:
            raise CommandValidationError("flatten sources must be siblings")
        parent_id = parent_ids.pop()
        siblings = _children(state, parent_id)
        positions = [siblings.index(item) for item in command.source_layer_ids]
        insert_at = min(positions)
        layers = state.layer_map
        removed: list[LayerId] = []
        for source in command.source_layer_ids:
            removed.extend(_descendants(source, layers))
        for source in set(removed):
            layers.pop(source, None)
        retained = tuple(item for item in siblings if item not in set(command.source_layer_ids))
        retained = retained[:insert_at] + (command.output_layer.layer_id,) + retained[insert_at:]
        if command.output_layer.layer_id in layers:
            raise CommandValidationError("flatten output layer ID already exists")
        layers[command.output_layer.layer_id] = command.output_layer
        roots = _replace_children(state, parent_id, retained, layers)
        effect = _require_resolved(
            state,
            context,
            command.output_asset.digest,
            command.output_asset.byte_count,
        )
        if effect is not None:
            effects.append(effect)
        used = _used_provenance(layers)
        candidate = _document(
            state,
            command.new_revision,
            layers=layers,
            roots=roots,
            assets=state.assets + (command.output_asset,),
            provenance=tuple(item for item in state.provenance if item in used),
        )
        changed = tuple(removed) + (command.output_layer.layer_id,)

    elif isinstance(command, ApplyOperationOutput):
        pixel_mode = command.output_asset is not None or command.output_layer is not None
        mask_mode = command.target_layer_id is not None or command.output_mask is not None
        if pixel_mode == mask_mode:
            raise CommandValidationError(
                "operation output must be exactly one pixel-layer or mask result"
            )
        provenance = list(state.provenance)
        if command.provenance not in provenance:
            provenance.append(command.provenance)
        if pixel_mode:
            if command.output_asset is None or command.output_layer is None:
                raise CommandValidationError("pixel operation output is incomplete")
            if command.output_layer.operation_provenance != command.provenance:
                raise CommandValidationError("pixel output provenance does not join")
            if command.output_layer.asset_digest != command.output_asset.digest:
                raise CommandValidationError("pixel output asset does not join")
            if command.output_asset.import_policy is not ImportPolicy.COPIED:
                raise CommandValidationError("operation output must be project-owned")
            if command.output_asset.digest in state.asset_map:
                raise CommandValidationError("operation output asset already exists")
            effect = _require_resolved(
                state,
                context,
                command.output_asset.digest,
                command.output_asset.byte_count,
            )
            if effect is not None:
                effects.append(effect)
            effects.extend(
                _mask_effect(state, context, command.output_layer.mask)
            )
            layers, roots = _insert_layer(
                state, command.output_layer, command.parent_id, command.index
            )
            candidate = _document(
                state,
                command.new_revision,
                layers=layers,
                roots=roots,
                assets=state.assets + (command.output_asset,),
                provenance=tuple(provenance),
            )
            changed = (command.output_layer.layer_id,)
        else:
            if command.target_layer_id is None or command.output_mask is None:
                raise CommandValidationError("mask operation output is incomplete")
            if command.output_mask.operation_provenance != command.provenance:
                raise CommandValidationError("mask output provenance does not join")
            layers = state.layer_map
            layer = layers.get(command.target_layer_id)
            if layer is None:
                raise CommandValidationError("mask target layer does not exist")
            effects.extend(_mask_effect(state, context, command.output_mask))
            layers[command.target_layer_id] = replace(layer, mask=command.output_mask)
            candidate = _document(
                state,
                command.new_revision,
                layers=layers,
                provenance=tuple(provenance),
            )
            changed = (command.target_layer_id,)

    else:  # pragma: no cover - COMMAND_TYPES closes this branch
        raise CommandValidationError("unsupported command type")

    effects.extend(_render_effects(changed))
    return ReductionResult(
        before_revision=state.revision_id,
        state=candidate,
        effects=tuple(effects),
        changed_layer_ids=changed,
    )
