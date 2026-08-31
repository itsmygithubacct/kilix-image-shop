from __future__ import annotations

import dataclasses
import unittest

from domain_fixtures import (
    empty_document,
    layer_id,
    object_id,
    provenance,
    sample_assets,
    sample_document,
)
from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.commands import (
    COMMAND_TYPES,
    AddLayer,
    ApplyOperationOutput,
    AttachMask,
    ChangeAdjustment,
    CommandValidationError,
    CropCanvas,
    EditText,
    EffectKind,
    FlattenLayers,
    ImportAsset,
    PaintMask,
    ReductionContext,
    RemoveLayer,
    ReorderLayer,
    ResolvedObject,
    RevisionConflict,
    SetLayerProperty,
    SetSelection,
    SetTransform,
    reduce_command,
)
from kilix_image_shop.domain.geometry import AffineTransform, Canvas
from kilix_image_shop.domain.identifiers import RevisionId
from kilix_image_shop.domain.layers import (
    Adjustment,
    AdjustmentId,
    BlendMode,
    FontAxis,
    FontFallback,
    MaskObject,
    MaskSource,
    Parameter,
    PixelLayer,
    Selection,
    SelectionKind,
    TextAlignment,
    TextLayout,
)


def revision(number: int) -> RevisionId:
    return RevisionId(f"00000000-0000-4000-8001-{number:012d}")


def copied_asset(character: str = "c", byte_count: int = 42) -> AssetRef:
    return AssetRef(
        digest=object_id(character),
        byte_count=byte_count,
        media_type=MediaType.PNG,
        width=8,
        height=6,
        profile_digest=object_id("1"),
        import_policy=ImportPolicy.COPIED,
    )


class CommandPopulationTests(unittest.TestCase):
    def test_command_family_population_is_exact_and_immutable(self) -> None:
        self.assertEqual(len(COMMAND_TYPES), 14)
        self.assertEqual(len(set(COMMAND_TYPES)), 14)
        command = SetSelection(
            expected_revision=empty_document().revision_id,
            new_revision=revision(1),
            selection=None,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command.new_revision = revision(2)  # type: ignore[misc]

    def test_stale_revision_fails_without_mutating_input(self) -> None:
        state = sample_document()
        before = state.canonical_bytes()
        command = SetSelection(
            expected_revision=revision(999),
            new_revision=revision(1),
            selection=None,
        )
        with self.assertRaises(RevisionConflict):
            reduce_command(state, command)
        self.assertEqual(state.canonical_bytes(), before)

    def test_new_revision_must_be_distinct(self) -> None:
        state = empty_document()
        with self.assertRaises(CommandValidationError):
            reduce_command(
                state,
                SetSelection(
                    expected_revision=state.revision_id,
                    new_revision=state.revision_id,
                    selection=None,
                ),
            )


class LayerStructureCommandTests(unittest.TestCase):
    def test_add_remove_and_reorder_layer_families(self) -> None:
        state = sample_document()
        added = PixelLayer(
            layer_id=layer_id(5), name="Added", asset_digest=object_id("6")
        )
        result = reduce_command(
            state,
            AddLayer(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                layer=added,
                parent_id=layer_id(4),
                index=1,
            ),
        )
        group = result.state.layer_map[layer_id(4)]
        self.assertEqual(group.child_layer_ids[1], added.layer_id)  # type: ignore[union-attr]
        self.assertEqual(len(result.effects), 2)

        reordered = reduce_command(
            result.state,
            ReorderLayer(
                expected_revision=result.state.revision_id,
                new_revision=revision(2),
                layer_id=added.layer_id,
                parent_id=layer_id(4),
                index=0,
            ),
        )
        group = reordered.state.layer_map[layer_id(4)]
        self.assertEqual(group.child_layer_ids[0], added.layer_id)  # type: ignore[union-attr]

        removed = reduce_command(
            reordered.state,
            RemoveLayer(
                expected_revision=reordered.state.revision_id,
                new_revision=revision(3),
                layer_id=added.layer_id,
            ),
        )
        self.assertNotIn(added.layer_id, removed.state.layer_map)

    def test_reorder_preserves_the_updated_root_table_across_group_boundaries(self) -> None:
        state = sample_document()
        moved_to_root = reduce_command(
            state,
            ReorderLayer(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                layer_id=layer_id(1),
                parent_id=None,
                index=1,
            ),
        )
        self.assertEqual(
            moved_to_root.state.root_layer_ids,
            (layer_id(4), layer_id(1)),
        )
        group = moved_to_root.state.layer_map[layer_id(4)]
        self.assertNotIn(layer_id(1), group.child_layer_ids)  # type: ignore[union-attr]

        moved_to_group = reduce_command(
            moved_to_root.state,
            ReorderLayer(
                expected_revision=moved_to_root.state.revision_id,
                new_revision=revision(2),
                layer_id=layer_id(1),
                parent_id=layer_id(4),
                index=0,
            ),
        )
        self.assertEqual(moved_to_group.state.root_layer_ids, (layer_id(4),))
        group = moved_to_group.state.layer_map[layer_id(4)]
        self.assertEqual(group.child_layer_ids.count(layer_id(1)), 1)  # type: ignore[union-attr]

    def test_nonempty_group_requires_explicit_recursive_remove(self) -> None:
        state = sample_document()
        with self.assertRaises(CommandValidationError):
            reduce_command(
                state,
                RemoveLayer(
                    expected_revision=state.revision_id,
                    new_revision=revision(1),
                    layer_id=layer_id(4),
                ),
            )
        result = reduce_command(
            state,
            RemoveLayer(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                layer_id=layer_id(4),
                recursive=True,
            ),
        )
        self.assertEqual(result.state.layers, ())
        self.assertEqual(result.state.provenance, ())

    def test_layer_property_adjustment_and_transform_families(self) -> None:
        state = sample_document()
        properties = reduce_command(
            state,
            SetLayerProperty(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                layer_id=layer_id(1),
                name="Renamed",
                visible=False,
                opacity_u16=12345,
                blend_mode=BlendMode.MULTIPLY,
            ),
        )
        layer = properties.state.layer_map[layer_id(1)]
        self.assertEqual(
            (layer.name, layer.visible, layer.opacity_u16, layer.blend_mode),
            ("Renamed", False, 12345, BlendMode.MULTIPLY),
        )

        adjustment = Adjustment(
            AdjustmentId.EXPOSURE, (Parameter("stops", -1.0),)
        )
        changed = reduce_command(
            properties.state,
            ChangeAdjustment(
                expected_revision=properties.state.revision_id,
                new_revision=revision(2),
                layer_id=layer_id(2),
                adjustment=adjustment,
            ),
        )
        self.assertEqual(changed.state.layer_map[layer_id(2)].adjustment, adjustment)  # type: ignore[union-attr]

        transformed = reduce_command(
            changed.state,
            SetTransform(
                expected_revision=changed.state.revision_id,
                new_revision=revision(3),
                layer_id=layer_id(1),
                transform=AffineTransform(e=4, f=5),
            ),
        )
        self.assertEqual(
            transformed.state.layer_map[layer_id(1)].transform,
            AffineTransform(e=4, f=5),
        )


class CanvasSelectionAndMaskCommandTests(unittest.TestCase):
    def test_crop_and_selection_families(self) -> None:
        state = sample_document()
        cropped = reduce_command(
            state,
            CropCanvas(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                canvas=Canvas(32, 32),
            ),
        )
        self.assertEqual(cropped.state.canvas, Canvas(32, 32))
        cleared = reduce_command(
            cropped.state,
            SetSelection(
                expected_revision=cropped.state.revision_id,
                new_revision=revision(2),
                selection=None,
            ),
        )
        self.assertIsNone(cleared.state.selection)
        selected = reduce_command(
            cleared.state,
            SetSelection(
                expected_revision=cleared.state.revision_id,
                new_revision=revision(3),
                selection=Selection(
                    SelectionKind.RASTER, object_id("d"), Canvas(4, 4).bounds
                ),
            ),
            ReductionContext((ResolvedObject(object_id("d"), 16),)),
        )
        self.assertIsNotNone(selected.state.selection)

    def test_paint_and_attach_mask_require_resolved_object_metadata(self) -> None:
        state = sample_document()
        mask = MaskObject(
            object_id("c"), 64, 48, 0, 0, MaskSource.HAND_PAINTED
        )
        command = PaintMask(
            expected_revision=state.revision_id,
            new_revision=revision(1),
            layer_id=layer_id(1),
            mask=mask,
            changed_tile_refs=(object_id("d"),),
        )
        with self.assertRaises(CommandValidationError):
            reduce_command(state, command)
        result = reduce_command(
            state,
            command,
            ReductionContext(
                (
                    ResolvedObject(mask.object_id, 64 * 48),
                    ResolvedObject(object_id("d"), 256),
                )
            ),
        )
        self.assertEqual(result.state.layer_map[layer_id(1)].mask, mask)
        self.assertEqual(result.effects[0].kind, EffectKind.WRITE_OBJECT)

        attached = reduce_command(
            result.state,
            AttachMask(
                expected_revision=result.state.revision_id,
                new_revision=revision(2),
                layer_id=layer_id(1),
                mask=None,
            ),
        )
        self.assertIsNone(attached.state.layer_map[layer_id(1)].mask)


class AssetTextFlattenAndOperationCommandTests(unittest.TestCase):
    def test_import_asset_requires_matching_resolved_bytes(self) -> None:
        state = empty_document()
        asset = copied_asset()
        layer = PixelLayer(
            layer_id=layer_id(5), name="Imported", asset_digest=asset.digest
        )
        command = ImportAsset(
            expected_revision=state.revision_id,
            new_revision=revision(1),
            asset=asset,
            layer=layer,
            parent_id=None,
            index=0,
        )
        with self.assertRaises(CommandValidationError):
            reduce_command(state, command, ReductionContext())
        with self.assertRaises(CommandValidationError):
            reduce_command(
                state,
                command,
                ReductionContext((ResolvedObject(asset.digest, asset.byte_count + 1),)),
            )
        result = reduce_command(
            state,
            command,
            ReductionContext((ResolvedObject(asset.digest, asset.byte_count),)),
        )
        self.assertEqual(result.state.assets, (asset,))
        self.assertEqual(result.state.root_layer_ids, (layer.layer_id,))
        self.assertEqual([effect.kind for effect in result.effects], [
            EffectKind.WRITE_OBJECT,
            EffectKind.INVALIDATE_PROXY,
            EffectKind.SCHEDULE_RENDER,
        ])

    def test_edit_text_preserves_editable_font_identity(self) -> None:
        state = sample_document()
        command = EditText(
            expected_revision=state.revision_id,
            new_revision=revision(1),
            layer_id=layer_id(3),
            text="Changed",
            layout=TextLayout(32, 16, TextAlignment.END, "en"),
            font_digest=object_id("f"),
            face_index=1,
            axes=(FontAxis("wght", 700),),
            fallbacks=(FontFallback("A", "B", None, "fixture"),),
            preview_asset_digest=object_id("7"),
        )
        with self.assertRaises(CommandValidationError):
            reduce_command(state, command)
        result = reduce_command(
            state,
            command,
            ReductionContext((ResolvedObject(object_id("f"), 123),)),
        )
        layer = result.state.layer_map[layer_id(3)]
        self.assertEqual((layer.text, layer.face_index), ("Changed", 1))  # type: ignore[union-attr]
        self.assertEqual(result.effects[0].kind, EffectKind.WRITE_OBJECT)

    def test_flatten_replaces_sibling_subtrees_with_one_pixel_layer(self) -> None:
        state = sample_document()
        asset = copied_asset()
        output = PixelLayer(
            layer_id=layer_id(5), name="Flattened", asset_digest=asset.digest
        )
        result = reduce_command(
            state,
            FlattenLayers(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                source_layer_ids=(layer_id(1), layer_id(2)),
                output_asset=asset,
                output_layer=output,
            ),
            ReductionContext((ResolvedObject(asset.digest, asset.byte_count),)),
        )
        group = result.state.layer_map[layer_id(4)]
        self.assertEqual(group.child_layer_ids, (output.layer_id, layer_id(3)))  # type: ignore[union-attr]
        self.assertNotIn(layer_id(1), result.state.layer_map)
        self.assertNotIn(layer_id(2), result.state.layer_map)
        self.assertEqual(result.state.provenance, ())

    def test_apply_operation_output_supports_exactly_pixel_or_mask_result(self) -> None:
        state = sample_document()
        operation = provenance()
        asset = copied_asset()
        output = PixelLayer(
            layer_id=layer_id(5),
            name="Operation output",
            asset_digest=asset.digest,
            operation_provenance=operation,
        )
        pixel_result = reduce_command(
            state,
            ApplyOperationOutput(
                expected_revision=state.revision_id,
                new_revision=revision(1),
                provenance=operation,
                output_asset=asset,
                output_layer=output,
                parent_id=layer_id(4),
                index=3,
            ),
            ReductionContext((ResolvedObject(asset.digest, asset.byte_count),)),
        )
        self.assertIn(output.layer_id, pixel_result.state.layer_map)

        mask = MaskObject(
            object_id("d"),
            64,
            48,
            0,
            0,
            MaskSource.OPERATION,
            operation_provenance=operation,
        )
        mask_result = reduce_command(
            pixel_result.state,
            ApplyOperationOutput(
                expected_revision=pixel_result.state.revision_id,
                new_revision=revision(2),
                provenance=operation,
                target_layer_id=layer_id(1),
                output_mask=mask,
            ),
            ReductionContext((ResolvedObject(mask.object_id, 64 * 48),)),
        )
        self.assertEqual(mask_result.state.layer_map[layer_id(1)].mask, mask)

        with self.assertRaises(CommandValidationError):
            reduce_command(
                state,
                ApplyOperationOutput(
                    expected_revision=state.revision_id,
                    new_revision=revision(3),
                    provenance=operation,
                ),
            )


if __name__ == "__main__":
    unittest.main()
