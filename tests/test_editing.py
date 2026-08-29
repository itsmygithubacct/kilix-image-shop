from __future__ import annotations

import ast
import dataclasses
import pathlib
import unittest

from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.commands import ReductionContext, ResolvedObject, reduce_command
from kilix_image_shop.domain.geometry import AffineTransform, GeometryLimits, Rect
from kilix_image_shop.domain.identifiers import LayerId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import (
    AdjustmentId,
    FontAxis,
    FontFallback,
    MaskObject,
    MaskSource,
    SelectionKind,
    TextAlignment,
    TextLayout,
)
from kilix_image_shop.editing.adjustments import (
    SCALAR_RULES,
    AdjustmentValidationError,
    add_adjustment_layer,
    change_adjustment,
    make_adjustment,
)
from kilix_image_shop.editing.masking import (
    attach_or_replace_mask,
    paint_mask,
    remove_mask,
)
from kilix_image_shop.editing.paint import (
    Brush,
    PaintLimits,
    Stroke,
    StrokePoint,
    StrokeTarget,
    pixel_stroke_layer_command,
    plan_stroke,
)
from kilix_image_shop.editing.selection import (
    clear_selection,
    make_selection,
    selection_to_mask,
    set_selection,
)
from kilix_image_shop.editing.text import (
    EditableText,
    TextValidationError,
    add_text_layer,
    edit_text_layer,
    font_digest,
    validate_font_payload,
)
from kilix_image_shop.editing.transforms import (
    compose,
    crop_canvas,
    rotation,
    scale,
    set_transform,
    translation,
)

from domain_fixtures import empty_document, layer_id, object_id, sample_document


def revision(number: int) -> RevisionId:
    return RevisionId(f"00000000-0000-4000-8000-{number:012d}")


class AdjustmentEditingTests(unittest.TestCase):
    def test_all_nine_closed_adjustment_definitions_validate_exact_parameters(self) -> None:
        populations = {
            AdjustmentId.EXPOSURE: {"stops": 1.5},
            AdjustmentId.CONTRAST: {"amount": 1.25},
            AdjustmentId.CURVES: {"points": (0.0, 0.0, 0.5, 0.6, 1.0, 1.0)},
            AdjustmentId.LEVELS: {
                "gamma": 1.0,
                "input-black": 0.0,
                "input-white": 1.0,
                "output-black": 0.0,
                "output-white": 1.0,
            },
            AdjustmentId.WHITE_BALANCE: {"temperature-k": 6500.0, "tint": 0.0},
            AdjustmentId.SATURATION: {"amount": 1.0},
            AdjustmentId.HUE: {"degrees": 0.0},
            AdjustmentId.SHARPEN: {
                "amount": 1.0,
                "radius": 2.0,
                "threshold": 0.1,
            },
            AdjustmentId.BLUR: {"sigma": 8.0},
        }
        self.assertEqual(set(populations), set(AdjustmentId))
        self.assertEqual(len(populations), 9)
        self.assertEqual(set(SCALAR_RULES) | {AdjustmentId.CURVES}, set(AdjustmentId))
        for adjustment_id, parameters in populations.items():
            with self.subTest(adjustment_id=adjustment_id):
                adjustment = make_adjustment(adjustment_id, parameters)
                self.assertEqual(adjustment.adjustment_id, adjustment_id)

        malformed = (
            (AdjustmentId.EXPOSURE, {"stops": 21.0}),
            (AdjustmentId.CONTRAST, {"wrong": 1.0}),
            (AdjustmentId.CURVES, {"points": (0.5, 0.0, 0.4, 1.0)}),
            (
                AdjustmentId.LEVELS,
                {
                    "gamma": 1.0,
                    "input-black": 1.0,
                    "input-white": 0.0,
                    "output-black": 0.0,
                    "output-white": 1.0,
                },
            ),
        )
        for adjustment_id, parameters in malformed:
            with self.subTest(malformed=adjustment_id), self.assertRaises(
                AdjustmentValidationError
            ):
                make_adjustment(adjustment_id, parameters)

    def test_adjustment_add_and_change_are_pure_reducible_commands(self) -> None:
        initial = empty_document()
        adjustment = make_adjustment(AdjustmentId.EXPOSURE, {"stops": 0.5})
        add = add_adjustment_layer(
            initial,
            new_revision=revision(3),
            layer_id=layer_id(10),
            name="Exposure",
            adjustment=adjustment,
            parent_id=None,
            index=0,
        )
        added = reduce_command(initial, add).state
        changed = change_adjustment(
            added,
            new_revision=revision(4),
            layer_id=layer_id(10),
            adjustment=make_adjustment(AdjustmentId.CONTRAST, {"amount": 1.5}),
        )
        final = reduce_command(added, changed).state
        self.assertEqual(
            final.layer_map[layer_id(10)].adjustment.adjustment_id,
            AdjustmentId.CONTRAST,
        )


class SelectionAndMaskEditingTests(unittest.TestCase):
    def test_selection_set_clear_and_selection_mask_conversion_are_lossless_refs(self) -> None:
        initial = empty_document()
        selection = make_selection(
            SelectionKind.RASTER,
            object_id("c"),
            Rect(2, 3, 8, 9),
        )
        command = set_selection(initial, new_revision=revision(3), selection=selection)
        selected = reduce_command(
            initial,
            command,
            ReductionContext((ResolvedObject(selection.object_id, 72),)),
        ).state
        mask = selection_to_mask(selection, object_id("d"))
        self.assertEqual(mask.source, MaskSource.SELECTION)
        self.assertEqual(mask.source_ref, selection.object_id)
        self.assertEqual(
            (mask.origin_x, mask.origin_y, mask.width, mask.height),
            (2, 3, 8, 9),
        )
        cleared = reduce_command(
            selected,
            clear_selection(selected, new_revision=revision(4)),
        ).state
        self.assertIsNone(cleared.selection)

    def test_mask_attach_paint_replace_and_remove_preserve_source_pixels(self) -> None:
        initial = sample_document()
        source_asset = initial.layer_map[layer_id(1)].asset_digest
        selection_mask = selection_to_mask(initial.selection, object_id("c"))
        attached = reduce_command(
            initial,
            attach_or_replace_mask(
                initial,
                new_revision=revision(3),
                layer_id=layer_id(1),
                mask=selection_mask,
            ),
            ReductionContext((ResolvedObject(selection_mask.object_id, 72),)),
        ).state
        hand_mask = MaskObject(
            object_id=object_id("d"),
            width=64,
            height=48,
            origin_x=0,
            origin_y=0,
            source=MaskSource.HAND_PAINTED,
        )
        tile_refs = (object_id("f"), object_id("e"))
        painted = reduce_command(
            attached,
            paint_mask(
                attached,
                new_revision=revision(4),
                layer_id=layer_id(1),
                mask=hand_mask,
                changed_tile_refs=tile_refs,
            ),
            ReductionContext(
                (
                    ResolvedObject(hand_mask.object_id, 64 * 48),
                    ResolvedObject(object_id("e"), 256),
                    ResolvedObject(object_id("f"), 256),
                )
            ),
        ).state
        self.assertEqual(painted.layer_map[layer_id(1)].asset_digest, source_asset)
        self.assertEqual(painted.layer_map[layer_id(1)].mask, hand_mask)
        removed = reduce_command(
            painted,
            remove_mask(painted, new_revision=revision(5), layer_id=layer_id(1)),
        ).state
        self.assertIsNone(removed.layer_map[layer_id(1)].mask)
        self.assertEqual(removed.layer_map[layer_id(1)].asset_digest, source_asset)


class TransformAndPaintEditingTests(unittest.TestCase):
    def test_transform_composition_and_crop_use_checked_document_geometry(self) -> None:
        initial = sample_document()
        transform = compose(translation(4.0, 8.0), scale(2.0, 3.0))
        self.assertEqual(transform, AffineTransform(a=2.0, d=3.0, e=4.0, f=8.0))
        self.assertAlmostEqual(rotation(90.0).b, 1.0)
        transformed = reduce_command(
            initial,
            set_transform(
                initial,
                new_revision=revision(3),
                layer_id=layer_id(1),
                transform=transform,
            ),
        ).state
        cropped = reduce_command(
            transformed,
            crop_canvas(
                transformed,
                new_revision=revision(4),
                rectangle=Rect(0, 0, 32, 32),
                limits=GeometryLimits(1000, 1000, 1_000_000),
            ),
        ).state
        self.assertEqual(cropped.canvas.bounds, Rect(0, 0, 32, 32))

    def test_mask_strokes_are_source_alpha_independent_and_tile_bounded(self) -> None:
        limits = PaintLimits(10, 16, 128.0)
        extent = Rect(0, 0, 513, 513)
        mask_brush = Brush(
            StrokeTarget.MASK,
            diameter=20.0,
            opacity_u16=65535,
            hardness_u16=65535,
            mask_value_u8=255,
        )
        mask_plan = plan_stroke(
            Stroke(mask_brush, (StrokePoint(255.0, 255.0),)),
            extent,
            limits,
        )
        self.assertFalse(mask_plan.uses_source_alpha)
        self.assertTrue(all(tile.width <= 256 and tile.height <= 256 for tile in mask_plan.tiles))
        self.assertEqual(len(mask_plan.tiles), 4)
        pixel_plan = plan_stroke(
            Stroke(
                Brush(
                    StrokeTarget.PIXELS,
                    diameter=2.0,
                    opacity_u16=65535,
                    hardness_u16=65535,
                    rgba_u16=(0, 0, 0, 65535),
                ),
                (StrokePoint(512.0, 512.0),),
            ),
            extent,
            limits,
        )
        self.assertTrue(pixel_plan.uses_source_alpha)
        self.assertEqual(len(pixel_plan.tiles), 4)
        self.assertIn(Rect(512, 512, 1, 1), pixel_plan.tiles)
        self.assertTrue(
            all(tile.width <= 256 and tile.height <= 256 for tile in pixel_plan.tiles)
        )

    def test_two_hundred_mask_strokes_have_finite_deterministic_tile_plans(self) -> None:
        extent = Rect(0, 0, 10_000, 10_000)
        limits = PaintLimits(1, 4, 32.0)
        brush = Brush(
            StrokeTarget.MASK,
            diameter=3.0,
            opacity_u16=65535,
            hardness_u16=32768,
            mask_value_u8=255,
        )
        plans = tuple(
            plan_stroke(
                Stroke(
                    brush,
                    (
                        StrokePoint(
                            float((index * 47) % extent.width),
                            float((index * 83) % extent.height),
                        ),
                    ),
                ),
                extent,
                limits,
            )
            for index in range(200)
        )
        self.assertEqual(len(plans), 200)
        self.assertTrue(all(1 <= len(plan.tiles) <= 4 for plan in plans))
        self.assertTrue(all(not plan.uses_source_alpha for plan in plans))

    def test_completed_pixel_stroke_becomes_a_new_immutable_layer(self) -> None:
        initial = empty_document()
        payload = b"painted-pixel"
        digest = ObjectId.from_bytes(payload)
        asset = AssetRef(
            digest=digest,
            byte_count=len(payload),
            media_type=MediaType.PNG,
            width=1,
            height=1,
            profile_digest=object_id("1"),
            import_policy=ImportPolicy.COPIED,
        )
        command = pixel_stroke_layer_command(
            initial,
            new_revision=revision(3),
            output_asset=asset,
            layer_id=layer_id(10),
            name="Paint stroke",
            parent_id=None,
            index=0,
        )
        result = reduce_command(
            initial,
            command,
            ReductionContext((ResolvedObject(digest, len(payload)),)),
        )
        self.assertEqual(result.state.layers[0].asset_digest, digest)
        self.assertEqual(initial.layers, ())


class TextEditingTests(unittest.TestCase):
    def editable(self, text: str = "Edited") -> EditableText:
        return EditableText(
            text=text,
            layout=TextLayout(32, 16, TextAlignment.START, "en"),
            font_digest=object_id("a"),
            face_index=0,
            axes=(FontAxis("wght", 500.0),),
            fallbacks=(FontFallback("Fixture Sans", "Fixture Sans", None, "exact"),),
            preview_asset_digest=object_id("7"),
        )

    def test_font_payload_and_editable_text_identity_remain_pinned(self) -> None:
        payload = b"synthetic-font"
        digest = font_digest(payload)
        validate_font_payload(payload, digest)
        with self.assertRaises(TextValidationError):
            validate_font_payload(payload + b"x", digest)

        initial = sample_document()
        edited = reduce_command(
            initial,
            edit_text_layer(
                initial,
                new_revision=revision(3),
                layer_id=layer_id(3),
                editable=self.editable(),
            ),
        ).state
        layer = edited.layer_map[layer_id(3)]
        self.assertEqual(layer.text, "Edited")
        self.assertEqual(layer.font_digest, object_id("a"))
        self.assertEqual(layer.axes, (FontAxis("wght", 500.0),))
        self.assertEqual(layer.preview_asset_digest, object_id("7"))

    def test_new_text_layer_stays_editable_instead_of_flattening(self) -> None:
        initial = sample_document()
        command = add_text_layer(
            initial,
            new_revision=revision(3),
            layer_id=layer_id(10),
            name="New text",
            editable=self.editable("Still editable"),
            parent_id=layer_id(4),
            index=3,
        )
        added = reduce_command(initial, command).state
        layer = added.layer_map[layer_id(10)]
        self.assertEqual(layer.text, "Still editable")
        self.assertEqual(layer.font_digest, object_id("a"))


class EditingDependencyTests(unittest.TestCase):
    def test_editing_functional_module_population_is_exact_and_native_free(self) -> None:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "kilix_image_shop"
            / "editing"
        )
        modules = tuple(
            sorted(path.name for path in root.glob("*.py") if path.name != "__init__.py")
        )
        self.assertEqual(
            modules,
            (
                "adjustments.py",
                "masking.py",
                "paint.py",
                "selection.py",
                "text.py",
                "transforms.py",
            ),
        )
        forbidden: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                forbidden.extend(
                    name
                    for name in names
                    if name.startswith(
                        ("gi", "kilix_image_shop.engine.runtime", "pathlib", "os")
                    )
                )
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
