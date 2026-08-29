from __future__ import annotations

import dataclasses
import math
import unittest

from domain_fixtures import compatibility, object_id, sample_assets
from kilix_image_shop.domain.assets import AssetRef, DecodeBudget, ImportPolicy, MediaType
from kilix_image_shop.domain.color import EngineCompatibility
from kilix_image_shop.domain.geometry import AffineTransform, Canvas, GeometryLimits, Rect
from kilix_image_shop.domain.identifiers import (
    DocumentId,
    DomainValidationError,
    ObjectId,
)


class IdentifierTests(unittest.TestCase):
    def test_uuid_identity_is_canonical_and_immutable(self) -> None:
        identity = DocumentId("00000000-0000-4000-8000-000000000115")
        self.assertEqual(str(identity), identity.value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            identity.value = "00000000-0000-4000-8000-000000000116"  # type: ignore[misc]

    def test_uuid_rejects_noncanonical_and_nonstring_values(self) -> None:
        for value in (
            "00000000-0000-4000-8000-00000000011A",
            "{00000000-0000-4000-8000-000000000115}",
            "not-a-uuid",
            115,
        ):
            with self.subTest(value=value), self.assertRaises(DomainValidationError):
                DocumentId.parse(value)

    def test_object_identity_binds_exact_bytes(self) -> None:
        self.assertEqual(
            ObjectId.from_bytes(b"kilix-image-shop\n").value,
            "a7323120abc3abd8d79352f22d9cb1fb9374ae40bd12b1b4166b8147751b48b2",
        )
        with self.assertRaises(DomainValidationError):
            ObjectId("A" * 64)


class GeometryTests(unittest.TestCase):
    def test_canvas_and_rectangle_are_checked(self) -> None:
        canvas = Canvas(10000, 10000, -10, -20)
        limits = GeometryLimits(20000, 20000, 100_000_000)
        limits.validate(canvas.width, canvas.height)
        self.assertTrue(Rect(-10, -20, 100, 100).is_within(canvas.bounds))

    def test_geometry_budget_refuses_large_or_invalid_values(self) -> None:
        limits = GeometryLimits(10000, 10000, 100_000_000)
        with self.assertRaises(DomainValidationError):
            limits.validate(10001, 1)
        with self.assertRaises(DomainValidationError):
            Canvas(0, 1)
        with self.assertRaises(DomainValidationError):
            Rect(0, 0, -1, 1)

    def test_affine_transform_refuses_nonfinite_and_singular_values(self) -> None:
        for transform in (
            (0, 0, 0, 0, 0, 0),
            (1, 0, 0, 1, math.inf, 0),
            (1, 0, 0, 1, math.nan, 0),
        ):
            with self.subTest(transform=transform), self.assertRaises(
                DomainValidationError
            ):
                AffineTransform(*transform)
        self.assertEqual(AffineTransform().to_data(), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0])


class AssetTests(unittest.TestCase):
    def test_copied_and_external_asset_path_policies_are_distinct(self) -> None:
        copied = sample_assets()[0]
        self.assertIsNone(copied.locator)
        external = AssetRef(
            digest=object_id("c"),
            byte_count=1,
            media_type=MediaType.PNG,
            width=1,
            height=1,
            profile_digest=object_id("1"),
            import_policy=ImportPolicy.EXTERNAL_PORTABLE_RELATIVE,
            locator="media/source.png",
        )
        self.assertEqual(AssetRef.from_data(external.to_data()), external)

    def test_asset_path_policy_refuses_traversal_and_mismatched_locator(self) -> None:
        common = {
            "digest": object_id("c"),
            "byte_count": 1,
            "media_type": MediaType.PNG,
            "width": 1,
            "height": 1,
            "profile_digest": object_id("1"),
        }
        with self.assertRaises(DomainValidationError):
            AssetRef(**common, import_policy=ImportPolicy.COPIED, locator="source.png")
        with self.assertRaises(DomainValidationError):
            AssetRef(
                **common,
                import_policy=ImportPolicy.EXTERNAL_PORTABLE_RELATIVE,
                locator="../source.png",
            )
        with self.assertRaises(DomainValidationError):
            AssetRef(
                **common,
                import_policy=ImportPolicy.EXTERNAL_ABSOLUTE,
                locator="source.png",
            )

    def test_decode_budget_checks_each_independent_counter(self) -> None:
        asset = sample_assets()[0]
        budget = DecodeBudget(128, 64, 48, 3072, 32, 1)
        budget.validate(asset, metadata_bytes=32)
        refusing = (
            DecodeBudget(127, 64, 48, 3072, 32, 1),
            DecodeBudget(128, 63, 48, 3072, 32, 1),
            DecodeBudget(128, 64, 48, 3071, 32, 1),
            DecodeBudget(128, 64, 48, 3072, 31, 1),
        )
        for candidate in refusing:
            with self.subTest(candidate=candidate), self.assertRaises(
                DomainValidationError
            ):
                candidate.validate(asset, metadata_bytes=32)


class CompatibilityTests(unittest.TestCase):
    def test_compatibility_round_trip_and_digest_are_stable(self) -> None:
        value = compatibility()
        decoded = EngineCompatibility.from_data(value.to_data())
        self.assertEqual(decoded, value)
        self.assertEqual(decoded.digest, value.digest)
        self.assertTrue(value.canonical_bytes().endswith(b"\n"))

    def test_deterministic_contract_refuses_ambient_values(self) -> None:
        value = compatibility()
        data = value.to_data()
        data["bablTolerance"] = "0.01"
        with self.assertRaises(DomainValidationError):
            EngineCompatibility.from_data(data)
        data = value.to_data()
        data["threads"] = 0
        with self.assertRaises(DomainValidationError):
            EngineCompatibility.from_data(data)

    def test_halo_records_are_canonicalized_and_unique(self) -> None:
        value = compatibility()
        data = value.to_data()
        data["tileHalos"] = [
            {"family": "sharpen", "pixels": 2},
            {"family": "blur", "pixels": 8},
        ]
        decoded = EngineCompatibility.from_data(data)
        self.assertEqual(decoded.tile_halos, (("blur", 8), ("sharpen", 2)))
        data["tileHalos"].append({"family": "blur", "pixels": 1})
        with self.assertRaises(DomainValidationError):
            EngineCompatibility.from_data(data)


if __name__ == "__main__":
    unittest.main()
