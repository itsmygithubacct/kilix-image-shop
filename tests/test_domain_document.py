from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import pathlib
import unittest

from domain_fixtures import (
    colour,
    compatibility,
    empty_document,
    layer_id,
    object_id,
    sample_assets,
    sample_document,
)
from kilix_image_shop.domain.document import DocumentState, PROJECT_SCHEMA
from kilix_image_shop.domain.geometry import Canvas, Rect
from kilix_image_shop.domain.identifiers import DomainValidationError, RevisionId
from kilix_image_shop.domain.layers import (
    GroupLayer,
    PixelLayer,
    Selection,
    SelectionKind,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "src" / "kilix_image_shop" / "domain"
SCHEMA = (
    ROOT
    / "src"
    / "kilix_image_shop"
    / "schemas"
    / "kilix.imageshop.project-v1.schema.json"
)
GOLDEN = ROOT / "tests" / "fixtures" / "domain" / "project-v1-empty.canonical.json"
GOLDEN_DIGEST = "442a48c905f50df485af1112485618f5aabb8557d6f378a52a98099151fa4b2b"


class ManifestTests(unittest.TestCase):
    def test_empty_golden_is_canonical_and_digest_bound(self) -> None:
        payload = GOLDEN.read_bytes()
        document = DocumentState.from_json_bytes(payload, max_manifest_bytes=65536)
        self.assertEqual(document, empty_document())
        self.assertEqual(document.canonical_bytes(), payload)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), GOLDEN_DIGEST)
        self.assertEqual(document.manifest_digest.value, GOLDEN_DIGEST)

    def test_full_layer_tree_round_trips_canonically(self) -> None:
        document = sample_document()
        payload = document.canonical_bytes()
        decoded = DocumentState.from_json_bytes(payload, max_manifest_bytes=len(payload))
        self.assertEqual(decoded, document)
        self.assertEqual(decoded.canonical_bytes(), payload)
        self.assertEqual(tuple(layer.layer_id for layer in decoded.layers), tuple(
            sorted((layer.layer_id for layer in decoded.layers), key=lambda item: item.value)
        ))

    def test_manifest_refuses_unknown_duplicate_and_nonfinite_members(self) -> None:
        value = empty_document().to_manifest()
        value["unknown"] = True
        with self.assertRaises(DomainValidationError):
            DocumentState.from_manifest(value)
        duplicate = b'{"schema":"kilix.imageshop.project/v1","schema":"other"}\n'
        with self.assertRaises(DomainValidationError):
            DocumentState.from_json_bytes(duplicate, max_manifest_bytes=1024)
        with self.assertRaises(DomainValidationError):
            DocumentState.from_json_bytes(b'{"value":NaN}\n', max_manifest_bytes=1024)

    def test_manifest_byte_budget_is_enforced_before_decode(self) -> None:
        payload = GOLDEN.read_bytes()
        with self.assertRaises(DomainValidationError):
            DocumentState.from_json_bytes(payload, max_manifest_bytes=len(payload) - 1)
        with self.assertRaises(DomainValidationError):
            DocumentState.from_json_bytes(b"\xff", max_manifest_bytes=1)

    def test_document_values_are_immutable(self) -> None:
        document = empty_document()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            document.revision_id = RevisionId(  # type: ignore[misc]
                "00000000-0000-4000-8000-000000000002"
            )


class TreeValidationTests(unittest.TestCase):
    def _document(
        self,
        *,
        roots: tuple = (),
        layers: tuple = (),
        assets: tuple = (),
        selection=None,
    ) -> DocumentState:
        return DocumentState(
            schema=PROJECT_SCHEMA,
            document_id=empty_document().document_id,
            revision_id=empty_document().revision_id,
            canvas=Canvas(64, 48),
            colour=colour(),
            engine_compatibility=compatibility(),
            assets=assets,
            root_layer_ids=roots,
            layers=layers,
            selection=selection,
        )

    def test_missing_assets_and_children_fail_closed(self) -> None:
        pixel = PixelLayer(
            layer_id=layer_id(1), name="Pixels", asset_digest=object_id("6")
        )
        with self.assertRaises(DomainValidationError):
            self._document(roots=(pixel.layer_id,), layers=(pixel,))
        group = GroupLayer(
            layer_id=layer_id(2), name="Group", child_layer_ids=(layer_id(99),)
        )
        with self.assertRaises(DomainValidationError):
            self._document(roots=(group.layer_id,), layers=(group,))

    def test_layer_must_be_exactly_one_root_or_child(self) -> None:
        pixel = PixelLayer(
            layer_id=layer_id(1), name="Pixels", asset_digest=object_id("6")
        )
        group = GroupLayer(
            layer_id=layer_id(2), name="Group", child_layer_ids=(pixel.layer_id,)
        )
        with self.assertRaises(DomainValidationError):
            self._document(
                roots=(group.layer_id, pixel.layer_id),
                layers=(group, pixel),
                assets=sample_assets(),
            )
        with self.assertRaises(DomainValidationError):
            self._document(layers=(pixel,), assets=sample_assets())

    def test_duplicate_parent_and_cycle_shapes_fail_closed(self) -> None:
        pixel = PixelLayer(
            layer_id=layer_id(1), name="Pixels", asset_digest=object_id("6")
        )
        first = GroupLayer(
            layer_id=layer_id(2), name="First", child_layer_ids=(pixel.layer_id,)
        )
        second = GroupLayer(
            layer_id=layer_id(3), name="Second", child_layer_ids=(pixel.layer_id,)
        )
        with self.assertRaises(DomainValidationError):
            self._document(
                roots=(first.layer_id, second.layer_id),
                layers=(pixel, first, second),
                assets=sample_assets(),
            )
        cycle_a = GroupLayer(
            layer_id=layer_id(4), name="A", child_layer_ids=(layer_id(5),)
        )
        cycle_b = GroupLayer(
            layer_id=layer_id(5), name="B", child_layer_ids=(layer_id(4),)
        )
        with self.assertRaises(DomainValidationError):
            self._document(layers=(cycle_a, cycle_b))

    def test_selection_cannot_leave_canvas(self) -> None:
        selection = Selection(
            SelectionKind.RASTER, object_id("b"), Rect(63, 47, 2, 2)
        )
        with self.assertRaises(DomainValidationError):
            self._document(selection=selection)

    def test_generated_layer_requires_document_provenance_ledger(self) -> None:
        document = sample_document()
        with self.assertRaises(DomainValidationError):
            dataclasses.replace(document, provenance=())


class SchemaAndBoundaryTests(unittest.TestCase):
    def test_schema_carrier_is_closed_and_names_four_layer_variants(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(schema["title"], PROJECT_SCHEMA)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(len(schema["$defs"]["layer"]["oneOf"]), 4)
        self.assertEqual(schema["$defs"]["mask"]["properties"]["format"], {"const": "Y u8"})
        self.assertEqual(
            schema["$defs"]["mask"]["properties"]["semantics"],
            {"const": "foreground-alpha"},
        )

    def test_every_local_schema_reference_resolves(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        references: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str):
                    references.append(reference)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertTrue(reference.startswith("#/"))
                value: object = schema
                for token in reference[2:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    self.assertIsInstance(value, dict)
                    value = value[token]  # type: ignore[index]
                self.assertIsInstance(value, dict)

    def test_domain_functional_module_population_is_exact(self) -> None:
        modules = {
            path.name
            for path in DOMAIN.glob("*.py")
            if path.name != "__init__.py"
        }
        self.assertEqual(
            modules,
            {
                "assets.py",
                "color.py",
                "commands.py",
                "document.py",
                "geometry.py",
                "identifiers.py",
                "layers.py",
            },
        )

    def test_domain_imports_only_standard_library_and_domain_modules(self) -> None:
        forbidden = {
            "gi",
            "Gegl",
            "Babl",
            "kilix_image_shop.engine",
            "kilix_image_shop.render",
            "kilix_image_shop.store",
            "kilix_image_shop.ops",
        }
        violations: list[str] = []
        for path in DOMAIN.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=path.as_posix())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if any(name == item or name.startswith(item + ".") for item in forbidden):
                        violations.append(f"{path.name}:{name}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
