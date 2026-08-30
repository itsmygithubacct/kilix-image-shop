from __future__ import annotations

import ast
import hashlib
import io
import json
import pathlib
import unittest

from domain_fixtures import colour, compatibility, layer_id

from kilix_image_shop.cli import commands, environment
from kilix_image_shop.cli.configuration import (
    ExitCode,
    default_project_limits,
    project_limits,
)
from kilix_image_shop.cli.main import main
from kilix_image_shop.cli.presentation import (
    OutputFormat,
    PresentationError,
    Report,
    counted,
    render,
    render_json,
    render_text,
)
from kilix_image_shop.domain.assets import AssetRef, ImportPolicy, MediaType
from kilix_image_shop.domain.document import PROJECT_SCHEMA, DocumentState
from kilix_image_shop.domain.geometry import Canvas
from kilix_image_shop.domain.identifiers import DocumentId, ObjectId, RevisionId
from kilix_image_shop.domain.layers import PixelLayer
from kilix_image_shop.export.presets import ExportFormat, ExportPreset, deterministic_preset
from kilix_image_shop.export.provenance import ExportArtifact, ExportProvenance
from kilix_image_shop.store.generations import GenerationStore, create_project
from kilix_image_shop.store.layout import ProjectLayout, StoreError

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCUMENT_ID = DocumentId("00000000-0000-4000-8000-000000000115")
FIRST_REVISION = RevisionId("00000000-0000-4000-8000-000000000001")
SECOND_REVISION = RevisionId("00000000-0000-4000-8000-000000000002")
ASSET_PAYLOAD = b"synthetic-cli-asset-payload"
ASSET_DIGEST = ObjectId(hashlib.sha256(ASSET_PAYLOAD).hexdigest())

INSTALLED_STATUS = """Package: libbabl-0.1-0
Status: install ok installed
Version: 1:0.1.114-2

Package: libgegl-0.4-0t64
Status: install ok installed
Version: 1:0.4.62-2+deb13u2

Package: python3-gi
Status: install ok installed
Version: 3.50.0-4+b1
"""


def document_with_asset() -> DocumentState:
    asset = AssetRef(
        digest=ASSET_DIGEST,
        byte_count=len(ASSET_PAYLOAD),
        media_type=MediaType.PNG,
        width=64,
        height=48,
        profile_digest=ObjectId("1" * 64),
        import_policy=ImportPolicy.COPIED,
    )
    layer = PixelLayer(layer_id=layer_id(1), name="Pixels", asset_digest=ASSET_DIGEST)
    return DocumentState(
        schema=PROJECT_SCHEMA,
        document_id=DOCUMENT_ID,
        revision_id=FIRST_REVISION,
        canvas=Canvas(64, 48),
        colour=colour(),
        engine_compatibility=compatibility(),
        assets=(asset,),
        root_layer_ids=(layer.layer_id,),
        layers=(layer,),
    )


def empty_second_revision() -> DocumentState:
    return DocumentState(
        schema=PROJECT_SCHEMA,
        document_id=DOCUMENT_ID,
        revision_id=SECOND_REVISION,
        canvas=Canvas(64, 48),
        colour=colour(),
        engine_compatibility=compatibility(),
        assets=(),
        root_layer_ids=(),
        layers=(),
    )


class CliHarness(unittest.TestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        out = io.StringIO()
        error = io.StringIO()
        code = main(list(arguments), stdout=out, stderr=error)
        return code, out.getvalue(), error.getvalue()

    def row(self, out: str, label: str) -> str:
        for line in out.splitlines():
            name, _, value = line.partition("  ")
            if name.strip() == label:
                return value.strip()
        self.fail(f"row {label!r} is absent from the rendered report")

    def make_project(self, root: pathlib.Path):
        limits = default_project_limits()
        return create_project(
            root,
            document_with_asset(),
            limits=limits,
            object_payloads={ASSET_DIGEST: ASSET_PAYLOAD},
        )


class ReportingTests(CliHarness):
    def test_version_reports_product_schema_and_engine_identity(self) -> None:
        code, out, error = self.run_cli("version")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(error, "")
        self.assertIn("kilix.imageshop.project/v1", out)
        self.assertIn("1:0.4.62-2+deb13u2", out)
        self.assertIn("0/2", out)

    def test_json_output_is_canonical_and_reparsable(self) -> None:
        code, out, _ = self.run_cli("--output", "json", "version")
        self.assertEqual(code, int(ExitCode.OK))
        value = json.loads(out)
        self.assertEqual(value["command"], "version")
        canonical = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        self.assertEqual(out, canonical)

    def test_text_and_json_renderings_are_byte_stable(self) -> None:
        report = Report("sample", (("b", "2"), ("a", "1")), {"b": 2, "a": 1})
        self.assertEqual(render_text(report), render_text(report))
        self.assertEqual(render_json(report), render_json(report))
        self.assertEqual(render_text(report), "b  2\na  1\n")
        self.assertEqual(render(report, OutputFormat.JSON), render_json(report))

    def test_presentation_refuses_unprintable_values_and_open_counts(self) -> None:
        with self.assertRaises(PresentationError):
            Report("sample", (("label", "line\nbreak"),), {})
        with self.assertRaises(PresentationError):
            counted(3, 2)
        self.assertEqual(counted(0, 2), "0/2")

    def test_operation_surfaces_report_zero_providers_and_eight_messages(self) -> None:
        code, out, _ = self.run_cli("ops", "providers")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(self.row(out, "providersInstalled"), "0/2")
        self.assertEqual(self.row(out, "operation.remove-background"), "unavailable")
        code, out, _ = self.run_cli("ops", "diagnostics")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(self.row(out, "messages"), "8/8")


class UsageTests(CliHarness):
    def test_missing_command_is_a_usage_failure_with_no_stdout(self) -> None:
        code, out, error = self.run_cli()
        self.assertEqual(code, int(ExitCode.USAGE))
        self.assertEqual(out, "")
        self.assertIn("usage", error)

    def test_unknown_command_is_refused_by_the_parser(self) -> None:
        code, out, error = self.run_cli("nonexistent")
        self.assertEqual(code, int(ExitCode.USAGE))
        self.assertEqual(out, "")
        self.assertTrue(error)

    def test_open_ceiling_override_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            project_limits(max_objects=0)
        with self.assertRaises(StoreError):
            project_limits(max_layers=-1)

    def test_missing_project_root_is_a_data_failure(self) -> None:
        with self.subTest("absent root"):
            code, out, error = self.run_cli("project", "info", "/nonexistent-f115-root")
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertEqual(out, "")
            self.assertIn("cannot be resolved", error)


class DoctorTests(CliHarness):
    def write_status(self, directory: pathlib.Path, body: str) -> pathlib.Path:
        path = directory / "status"
        path.write_text(body, encoding="utf-8")
        return path

    def ready_environment(self, directory: pathlib.Path) -> dict[str, object]:
        origin = directory / "gi-origin.py"
        origin.write_text("", encoding="utf-8")
        return {
            "status_path": self.write_status(directory, INSTALLED_STATUS),
            "environ": {"BABL_TOLERANCE": "0.0"},
            "isolated": 1,
            "gi_origin": origin,
        }

    def test_complete_group_reports_ready_across_four_required_classes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            outcome = commands.doctor_command(**self.ready_environment(directory))
            self.assertEqual(outcome.exit_code, ExitCode.OK)
            self.assertEqual(outcome.report.data["requiredReady"], 7)
            self.assertEqual(outcome.report.data["requiredTotal"], 7)
            self.assertTrue(outcome.report.data["conventionalEditingReady"])

    def test_absent_engine_group_is_unavailable_and_names_each_package(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            keywords = self.ready_environment(directory)
            keywords["status_path"] = self.write_status(
                directory,
                "Package: python3-gi\nStatus: install ok installed\nVersion: 3.50.0-4+b1\n",
            )
            outcome = commands.doctor_command(**keywords)
            self.assertEqual(outcome.exit_code, ExitCode.UNAVAILABLE)
            self.assertEqual(outcome.report.data["requiredReady"], 5)
            states = {
                item["component"]: item["state"]
                for item in outcome.report.data["components"]
            }
            self.assertEqual(states["package.libgegl-0.4-0t64"], "missing")
            self.assertEqual(states["package.libbabl-0.1-0"], "missing")
            self.assertEqual(states["package.python3-gi"], "ready")

    def test_version_drift_is_reported_as_mismatched_not_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            keywords = self.ready_environment(directory)
            keywords["status_path"] = self.write_status(
                directory,
                INSTALLED_STATUS.replace("1:0.1.114-2", "1:0.1.113-1"),
            )
            outcome = commands.doctor_command(**keywords)
            self.assertEqual(outcome.exit_code, ExitCode.UNAVAILABLE)
            states = {
                item["component"]: item["state"]
                for item in outcome.report.data["components"]
            }
            self.assertEqual(states["package.libbabl-0.1-0"], "mismatched")

    def test_providers_and_toolkit_are_deferred_and_never_gate_readiness(self) -> None:
        deferred = environment.provider_reports() + environment.presentation_reports()
        self.assertEqual(len(deferred), 3)
        for item in deferred:
            self.assertFalse(item.required)
            self.assertEqual(item.state, environment.ComponentState.DEFERRED)


class ProjectTests(CliHarness):
    def test_info_reports_ten_validation_classes_and_the_object_closure(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            layout, generation = self.make_project(root)
            code, out, error = self.run_cli("--output", "json", "project", "info", str(root))
            self.assertEqual(code, int(ExitCode.OK), error)
            value = json.loads(out)["result"]
            self.assertEqual(value["documentId"], DOCUMENT_ID.value)
            self.assertEqual(value["headGeneration"], generation.generation_id.value)
            self.assertEqual(len(value["validatedClasses"]), 10)
            self.assertEqual(value["closure"]["objectCount"], 1)
            self.assertEqual(value["closure"]["byteCount"], len(ASSET_PAYLOAD))
            self.assertEqual(value["limits"]["maxLayers"], default_project_limits().max_layers)
            self.assertEqual(layout.root, root)

    def test_verify_redigests_the_closure_and_fails_on_a_corrupt_object(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            layout, _ = self.make_project(root)
            code, out, error = self.run_cli("project", "verify", str(root))
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "closureVerified"), "1/1")
            self.assertEqual(self.row(out, "failures"), "0/1")

            carrier = layout.object_path(ASSET_DIGEST)
            carrier.chmod(0o600)
            carrier.write_bytes(b"tampered-payload-of-the-same-length-x")
            code, out, error = self.run_cli("project", "verify", str(root))
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertEqual(out, "")
            self.assertIn("project cannot be opened", error)

    def test_generations_marks_head_and_recovery_selects_only_under_apply(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            layout, first = self.make_project(root)
            store = GenerationStore(layout, default_project_limits())
            second = store.save(
                empty_second_revision(),
                object_payloads={},
                expected_head=first.generation_id,
            )

            code, out, error = self.run_cli("--output", "json", "project", "generations", str(root))
            self.assertEqual(code, int(ExitCode.OK), error)
            value = json.loads(out)["result"]
            self.assertEqual(value["head"], second.generation_id.value)
            self.assertEqual(len(value["generations"]), 2)
            self.assertEqual(
                {item["generation"] for item in value["generations"] if item["isHead"]},
                {second.generation_id.value},
            )

            code, out, error = self.run_cli(
                "project", "recover", str(root), first.generation_id.value
            )
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "mode"), "preview")
            self.assertEqual(self.row(out, "headReplaced"), "0/1")
            from kilix_image_shop.store.generations import read_head

            self.assertEqual(read_head(ProjectLayout(root)), second.generation_id)

            code, out, error = self.run_cli(
                "project", "recover", str(root), first.generation_id.value, "--apply"
            )
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "mode"), "applied")
            self.assertEqual(read_head(ProjectLayout(root)), first.generation_id)

    def test_recovering_an_unknown_generation_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            self.make_project(root)
            code, out, error = self.run_cli(
                "project", "recover", str(root), "0" * 64, "--apply"
            )
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertEqual(out, "")
            self.assertIn("not selectable", error)

    def test_collection_previews_before_it_quarantines_anything(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            layout, first = self.make_project(root)
            store = GenerationStore(layout, default_project_limits())
            store.save(
                empty_second_revision(),
                object_payloads={},
                expected_head=first.generation_id,
            )

            code, out, error = self.run_cli("--output", "json", "project", "gc", str(root))
            self.assertEqual(code, int(ExitCode.OK), error)
            value = json.loads(out)["result"]
            self.assertEqual(value["unreachableObjectCount"], 1)
            self.assertEqual(value["unreachableByteCount"], len(ASSET_PAYLOAD))
            self.assertEqual(value["rootGenerationCount"], 1)
            self.assertEqual(value["retainedGenerationCount"], 2)
            self.assertFalse(value["applied"])
            self.assertEqual(value["quarantinedObjectCount"], 0)
            self.assertTrue(layout.object_path(ASSET_DIGEST).exists())

            code, out, error = self.run_cli("--output", "json", "project", "gc", str(root), "--apply")
            self.assertEqual(code, int(ExitCode.OK), error)
            value = json.loads(out)["result"]
            self.assertTrue(value["applied"])
            self.assertEqual(value["quarantinedObjectCount"], 1)
            self.assertFalse(layout.object_path(ASSET_DIGEST).exists())

            code, out, error = self.run_cli("project", "verify", str(root))
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "closureVerified"), "0/0")


class ExportTests(CliHarness):
    def preset_for(self, generation_id: ObjectId) -> ExportPreset:
        return deterministic_preset(
            document_with_asset(),
            generation_id,
            ExportFormat.PNG,
        )

    def test_preset_binds_the_current_generation_without_rendering_pixels(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            _, generation = self.make_project(root)
            carrier = pathlib.Path(temporary) / "preset.json"
            code, out, error = self.run_cli(
                "--output",
                "json",
                "export",
                "preset",
                str(root),
                "png",
                "--out",
                str(carrier),
            )
            self.assertEqual(code, int(ExitCode.OK), error)
            value = json.loads(out)["result"]
            expected = self.preset_for(generation.generation_id)
            self.assertEqual(value["presetSha256"], expected.digest.value)
            self.assertEqual(value["renderedPixels"], 0)
            self.assertEqual(carrier.read_bytes(), expected.canonical_bytes())

            code, out, error = self.run_cli(
                "export", "preset", str(root), "png", "--out", str(carrier)
            )
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertIn("cannot be written", error)

    def test_unknown_export_format_is_a_usage_failure(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "project"
            self.make_project(root)
            code, out, error = self.run_cli("export", "preset", str(root), "avif")
            self.assertEqual(code, int(ExitCode.USAGE))
            self.assertEqual(out, "")
            self.assertIn("outside the closed set", error)

    def sidecar_for(self, preset: ExportPreset, payload: bytes) -> ExportProvenance:
        document = document_with_asset()
        artifact = ExportArtifact(
            image_digest=ObjectId.from_bytes(payload),
            byte_count=len(payload),
            export_format=preset.export_format,
            width=preset.width,
            height=preset.height,
            profile_digest=preset.output_profile,
            metadata_keys=(),
        )
        return ExportProvenance(
            schema="kilix.imageshop.export-provenance/v1",
            document_id=preset.document_id,
            revision=preset.revision,
            document_manifest_digest=preset.document_manifest_digest,
            generation_digest=preset.generation_digest,
            object_closure_digest=preset.object_closure_digest,
            render_plan_digest=ObjectId("c" * 64),
            preset_digest=preset.digest,
            engine_compatibility=document.engine_compatibility,
            artifact=artifact,
            operations=(),
        )

    def test_sidecar_joins_its_preset_and_rejects_tampered_artifact_bytes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            payload = b"exported-artifact-bytes"
            preset = self.preset_for(ObjectId("d" * 64))
            sidecar = self.sidecar_for(preset, payload)
            preset_path = directory / "preset.json"
            sidecar_path = directory / "sidecar.json"
            artifact_path = directory / "artifact.png"
            preset_path.write_bytes(preset.canonical_bytes())
            sidecar_path.write_bytes(sidecar.canonical_bytes())
            artifact_path.write_bytes(payload)

            code, out, error = self.run_cli(
                "export", "verify", str(sidecar_path), str(preset_path)
            )
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "artifactBytesChecked"), "0/1")
            self.assertEqual(self.row(out, "checks"), "2/2")

            code, out, error = self.run_cli(
                "export",
                "verify",
                str(sidecar_path),
                str(preset_path),
                "--artifact",
                str(artifact_path),
            )
            self.assertEqual(code, int(ExitCode.OK), error)
            self.assertEqual(self.row(out, "artifactBytesChecked"), "1/1")
            self.assertEqual(self.row(out, "checks"), "4/4")

            artifact_path.write_bytes(b"tampered-artifact-bytes")
            code, out, error = self.run_cli(
                "export",
                "verify",
                str(sidecar_path),
                str(preset_path),
                "--artifact",
                str(artifact_path),
            )
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertEqual(out, "")
            self.assertIn("digest differs", error)

    def test_sidecar_bound_to_another_preset_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            payload = b"exported-artifact-bytes"
            sidecar = self.sidecar_for(self.preset_for(ObjectId("d" * 64)), payload)
            other = self.preset_for(ObjectId("e" * 64))
            preset_path = directory / "preset.json"
            sidecar_path = directory / "sidecar.json"
            preset_path.write_bytes(other.canonical_bytes())
            sidecar_path.write_bytes(sidecar.canonical_bytes())
            code, out, error = self.run_cli(
                "export", "verify", str(sidecar_path), str(preset_path)
            )
            self.assertEqual(code, int(ExitCode.INVALID_DATA))
            self.assertEqual(out, "")
            self.assertIn("does not join its preset", error)


class CommandSurfaceBoundaryTests(unittest.TestCase):
    def test_command_surface_is_exactly_five_modules(self) -> None:
        root = ROOT / "src" / "kilix_image_shop" / "cli"
        actual = {
            path.name for path in root.glob("*.py") if path.name != "__init__.py"
        }
        self.assertEqual(
            actual,
            {
                "commands.py",
                "configuration.py",
                "environment.py",
                "main.py",
                "presentation.py",
            },
        )

    def test_command_surface_imports_zero_toolkit_or_provider_modules(self) -> None:
        forbidden = ("gi", "gtk", "qt", "tkinter", "PyQt", "wx", "providers")
        root = ROOT / "src" / "kilix_image_shop" / "cli"
        offenders: list[str] = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = (node.module,)
                else:
                    continue
                for name in names:
                    head = name.split(".")[0]
                    if head in forbidden or name.endswith(forbidden[-1]):
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])

    def test_every_exit_status_comes_from_the_closed_set(self) -> None:
        self.assertEqual(
            {int(item) for item in ExitCode},
            {0, 1, 2, 3, 4},
        )


if __name__ == "__main__":
    unittest.main()
