from __future__ import annotations

import unittest

from domain_fixtures import layer_id, object_id, provenance, sample_document
from kilix_image_shop.domain.geometry import AffineTransform, Rect
from kilix_image_shop.domain.identifiers import DomainValidationError
from kilix_image_shop.domain.layers import (
    Adjustment,
    AdjustmentId,
    AdjustmentLayer,
    BlendMode,
    FontAxis,
    FontFallback,
    GroupLayer,
    MaskObject,
    MaskSource,
    OperationProvenance,
    Parameter,
    PixelLayer,
    Selection,
    SelectionKind,
    TextAlignment,
    TextLayer,
    TextLayout,
    layer_from_data,
    layer_to_data,
)


class ParameterTests(unittest.TestCase):
    def test_parameters_are_sorted_and_vectors_are_immutable(self) -> None:
        adjustment = Adjustment(
            AdjustmentId.CURVES,
            (
                Parameter("points", [0, 0.0, 1, 1.0]),
                Parameter("channel", "rgb"),
            ),
        )
        self.assertEqual(
            tuple(item.name for item in adjustment.parameters), ("channel", "points")
        )
        self.assertEqual(adjustment.parameters[1].value, (0.0, 0.0, 1.0, 1.0))

    def test_parameters_reject_duplicates_nonfinite_and_nested_values(self) -> None:
        with self.assertRaises(DomainValidationError):
            Adjustment(
                AdjustmentId.EXPOSURE,
                (Parameter("stops", 1), Parameter("stops", 2)),
            )
        for value in (float("nan"), float("inf"), {"nested": True}, []):
            with self.subTest(value=value), self.assertRaises(DomainValidationError):
                Parameter("value", value)  # type: ignore[arg-type]


class MaskAndProvenanceTests(unittest.TestCase):
    def test_mask_sources_require_exact_provenance_shape(self) -> None:
        painted = MaskObject(
            object_id("9"), 64, 48, 0, 0, MaskSource.HAND_PAINTED
        )
        self.assertEqual(MaskObject.from_data(painted.to_data()), painted)
        with self.assertRaises(DomainValidationError):
            MaskObject(
                object_id("9"),
                64,
                48,
                0,
                0,
                MaskSource.OPERATION,
            )
        operation = MaskObject(
            object_id("9"),
            64,
            48,
            0,
            0,
            MaskSource.OPERATION,
            operation_provenance=provenance(),
        )
        self.assertIsInstance(operation.operation_provenance, OperationProvenance)

    def test_operation_provenance_round_trips_without_provider_prose(self) -> None:
        value = provenance()
        self.assertEqual(OperationProvenance.from_data(value.to_data()), value)
        data = value.to_data()
        data["provider"] = "not a closed id"
        with self.assertRaises(DomainValidationError):
            OperationProvenance.from_data(data)


class LayerTests(unittest.TestCase):
    def test_all_closed_layer_variants_round_trip(self) -> None:
        document = sample_document()
        self.assertEqual(len(document.layers), 4)
        expected_types = {"pixel", "adjustment", "text", "group"}
        serialized_types = {layer_to_data(layer)["type"] for layer in document.layers}
        self.assertEqual(serialized_types, expected_types)
        for layer in document.layers:
            with self.subTest(layer=layer.layer_id):
                self.assertEqual(layer_from_data(layer_to_data(layer)), layer)

    def test_unknown_layer_types_and_fields_fail_closed(self) -> None:
        pixel = PixelLayer(
            layer_id=layer_id(1), name="Pixels", asset_digest=object_id("6")
        )
        data = layer_to_data(pixel)
        data["type"] = "native-gegl-operation"
        with self.assertRaises(DomainValidationError):
            layer_from_data(data)
        data = layer_to_data(pixel)
        data["operationName"] = "gegl:arbitrary"
        with self.assertRaises(DomainValidationError):
            layer_from_data(data)

    def test_group_children_are_typed_unique_and_not_self_referential(self) -> None:
        with self.assertRaises(DomainValidationError):
            GroupLayer(
                layer_id=layer_id(1),
                name="Duplicate",
                child_layer_ids=(layer_id(2), layer_id(2)),
            )
        with self.assertRaises(DomainValidationError):
            GroupLayer(
                layer_id=layer_id(1),
                name="Self",
                child_layer_ids=(layer_id(1),),
            )

    def test_layer_opacity_and_transform_are_bounded(self) -> None:
        with self.assertRaises(DomainValidationError):
            PixelLayer(
                layer_id=layer_id(1),
                name="Pixels",
                asset_digest=object_id("6"),
                opacity_u16=65536,
            )
        with self.assertRaises(DomainValidationError):
            PixelLayer(
                layer_id=layer_id(1),
                name="Pixels",
                asset_digest=object_id("6"),
                transform=AffineTransform(0, 0, 0, 0, 0, 0),
            )

    def test_text_identity_preserves_font_axes_fallback_and_preview(self) -> None:
        layer = TextLayer(
            layer_id=layer_id(3),
            name="Text",
            text="Editable",
            layout=TextLayout(20, 10, TextAlignment.CENTER, "en"),
            font_digest=object_id("a"),
            face_index=2,
            axes=(FontAxis("wght", 650),),
            fallbacks=(FontFallback("A", "B", object_id("c"), "missing A"),),
            preview_asset_digest=object_id("7"),
        )
        self.assertEqual(layer_from_data(layer_to_data(layer)), layer)

    def test_selection_is_typed_and_bounded_geometry(self) -> None:
        selection = Selection(SelectionKind.RASTER, object_id("b"), Rect(1, 2, 3, 4))
        self.assertEqual(Selection.from_data(selection.to_data()), selection)


if __name__ == "__main__":
    unittest.main()
